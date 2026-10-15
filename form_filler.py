"""Dynamic form filler (Playwright).

Discovers every field on a page (input/select/textarea, custom dropdowns,
shadow DOM, iframes), infers a label for each one from the page source and
matches it to the keys of a data row by name similarity. Handles multi-step
forms, logging in first, CAPTCHA pauses (a human solves them), file uploads,
fake data for missing fields, validation-aware retries and batch runs over
many URLs x many data rows x many accounts (optionally in parallel).

Run from PyCharm with no arguments to use test_form.html and data.json.
"""
import argparse
import json
import re
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, Frame

from datagen import DataGen
from inputs import load_table, load_urls, load_logins, parse_urls, to_file_uri_if_path

BASE = Path(__file__).resolve().parent
DEFAULT_URL = (BASE / "test_form.html").as_uri()
DEFAULT_DATA = BASE / "data.json"
DEFAULT_OUT = BASE / "output"
THRESHOLD = 0.65
MAX_STEPS = 10  # max steps (pages) of one form


@dataclass
class Settings:
    headless: bool = False
    slow: int = 300
    submit: bool = True
    next_words: list = field(default_factory=lambda: [
        "next", "continue", "proceed", "forward"])
    submit_words: list = field(default_factory=lambda: [
        "submit", "send", "register", "signup", "sign up", "login", "log in",
        "signin", "sign in", "save", "apply", "create", "finish", "confirm"])
    next_selector: str | None = None
    submit_selector: str | None = None
    fake_missing: bool = False   # generate values for fields without data
    faker_locale: str = "en_US"
    retries: int = 2             # re-submit attempts after validation errors
    interactive: bool = False    # a human can press Enter in this terminal
    captcha_timeout: int = 180   # seconds to wait for a human to solve a CAPTCHA
    cookies: str = "accept"      # cookie banners: accept | reject | ignore
    hold: float | None = None    # seconds to keep the browser open at the end
    job_pause: float = 2.0       # seconds to wait after each job (visible runs)
    highlight: bool = True       # outline the field being filled (visible runs)
    debug: bool = False          # print the signals and top candidates of every field
    inspect: bool = False        # only list the fields, do not fill anything
    field_map: dict = field(default_factory=dict)  # {"css selector": "key" | "=literal"}
    out: Path = DEFAULT_OUT
    cookies_in: str | None = None    # load cookies from this JSON file
    cookies_out: str | None = None   # save cookies to this JSON file


CFG = Settings()

# Small synonym dictionary: normalized data key -> alternative field names.
ALIASES = {
    "fullname": ["name", "yourname", "customername", "contactname", "displayname",
                 "completename"],
    "firstname": ["givenname", "forename", "fname", "first"],
    "lastname": ["surname", "familyname", "lname", "last"],
    "email": ["mail", "emailaddress", "useremail", "regemail"],
    "phone": ["telephone", "tel", "mobile", "phonenumber", "cell", "msisdn"],
    "birthdate": ["dob", "birthday", "dateofbirth", "bday", "birth"],
    "city": ["town"],
    "country": ["nation"],
    "address": ["street", "streetaddress"],
    "message": ["comment", "comments", "notes", "description"],
    "gender": ["sex"],
    "username": ["user", "login", "userid", "uname", "usr"],
    "password": ["pass", "pwd", "passwd", "regpasswd"],
    "resume": ["cv", "attachment", "upload"],
}

# HTML autocomplete="..." tokens and input types are strong, label-free hints.
AUTOCOMPLETE_HINTS = {
    "given-name": "firstname", "family-name": "lastname", "email": "email",
    "tel": "phone", "bday": "birthdate", "country": "country", "country-name": "country",
    "street-address": "address", "address-line1": "address", "address-level2": "city",
    "username": "username", "current-password": "password", "new-password": "password",
}
KIND_HINTS = {"email": "email", "tel": "phone", "password": "password"}

CAPTCHA_SELECTOR = (".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], "
                    "iframe[src*='captcha'], iframe[src*='turnstile']")

import random

# Realni UA stringovi — ne izmišljaj ih, ovi se poklapaju s pravim verzijama.
USER_AGENTS = [
    # Chrome 131 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 130 — Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 129 — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Edge 131 — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

# Veličine viewporta koje odgovaraju realnim rezolucijama.
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 2560, "height": 1440},
    {"width": 1680, "height": 1050},
]

# Realne vremenske zone i jezici — moraju odgovarati IP lokaciji!
LOCALES = [
    ("en-US", "America/New_York"),
    ("en-GB", "Europe/London"),
    ("hr-HR", "Europe/Zagreb"),
]


def random_ua() -> str:
    return random.choice(USER_AGENTS)


def random_viewport() -> dict:
    return random.choice(VIEWPORTS)


def random_locale() -> tuple[str, str]:
    return random.choice(LOCALES)

STEALTH_JS = """
() => {
  // 1. navigator.webdriver — najosnovniji signal
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // 2. navigator.plugins — headless Chrome ima prazan array
  Object.defineProperty(navigator, 'plugins', {
    get: () => [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer' },
      { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer' },
    ],
  });

  // 3. navigator.languages — mora odgovarati localeu
  Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
  });

  // 4. navigator.hardwareConcurrency — realni brojevi su 4, 8, 12, 16
  Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
  });

  // 5. navigator.deviceMemory — realno 4 ili 8
  Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
  });

  // 6. navigator.platform — mora odgovarati UA stringu
  Object.defineProperty(navigator, 'platform', {
    get: () => 'Win32',
  });

  // 7. chrome.runtime — postoji u pravom Chromeu
  window.chrome = {
    runtime: {},
    loadTimes: function () {},
    csi: function () {},
    app: {},
  };

  // 8. Permissions API — headless vraća 'denied' za notifications
  const originalQuery = window.navigator.permissions.query;
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );

  // 9. WebGL vendor/renderer — headless vraća 'Google Inc.' ili prazno
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) return 'Intel Inc.';       // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return getParameter.apply(this, arguments);
  };

  // 10. Canvas fingerprint — dodaj mikrošum da hash bude jedinstven
  const toDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function () {
    const context = this.getContext('2d');
    if (context) {
      const imageData = context.getImageData(0, 0, this.width, this.height);
      for (let i = 0; i < imageData.data.length; i += 100) {
        imageData.data[i] = imageData.data[i] ^ 1;  // promijeni 1 bit svakih 100 bajtova
      }
      context.putImageData(imageData, 0, 0);
    }
    return toDataURL.apply(this, arguments);
  };

  // 12. navigator.connection — realni browser ima
  Object.defineProperty(navigator, 'connection', {
    get: () => ({
      effectiveType: '4g',
      rtt: 50,
      downlink: 10,
      saveData: false,
    }),
  });

  // 13. Izbriši __playwright i __pw_manual tragove
  delete window.__playwright;
  delete window.__pw_manual;
  delete window.__PW_inspect;
}
"""


# JS run in every frame: find visible, enabled fields (also inside open shadow
# roots) and infer their labels from the page source. Tags each field with
# data-ff-idx. Includes custom dropdowns (role=combobox / aria-haspopup=listbox).
DISCOVER_JS = """
() => {
  const COMBO = '[role=combobox], [aria-haspopup=listbox]';
  const FORMISH = 'input, select, textarea, button, script, style';
  const dedupe = s => {  // "Ime Ime Prezime" -> "Ime Prezime"
    const out = [];
    for (const w of s.split(' '))
      if (!out.length || out[out.length - 1].toLowerCase() !== w.toLowerCase()) out.push(w);
    return out.join(' ');
  };
  const clean = s => dedupe((s || '').replace(/\\s+/g, ' ').trim());
  const deepAll = (root, sel, out = []) => {   // querySelectorAll through shadow roots
    root.querySelectorAll(sel).forEach(e => out.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deepAll(e.shadowRoot, sel, out); });
    return out;
  };
  deepAll(document, '[data-ff-idx]').forEach(e => e.removeAttribute('data-ff-idx'));
  // Visible text nodes joined by spaces (adjacent <span>/<div> no longer glue together).
  const spaced = (node, skipControls) => {
    const parts = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const p = n.parentElement;
      if (!p) continue;
      if (p.closest('script, style, noscript')) continue;
      if (skipControls && p.closest('select, textarea, option')) continue;
      const cs = getComputedStyle(p);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      parts.push(n.textContent);
    }
    return clean(parts.join(' '));
  };
  const own = l => spaced(l, true);  // label text without nested controls
  // Structural (language independent) signals of an element.
  const attr = (n, a) => (n.getAttribute && n.getAttribute(a)) || '';
  const dataOf = el => [...el.attributes]
    .filter(a => a.name.startsWith('data-') && a.name !== 'data-ff-idx')
    .map(a => a.name.slice(5) + ' ' + a.value.slice(0, 60)).join(' ');
  const ctxOf = el => {  // classes/ids of the nearest wrappers, up to the form
    const out = [];
    let p = el.parentElement;
    for (let i = 0; p && i < 3 && p.tagName !== 'FORM' && p.tagName !== 'BODY'; i++, p = p.parentElement) {
      out.push(attr(p, 'class'));
      if (p.id) out.push(p.id);
    }
    return out.join(' ');
  };
  // Text that visually precedes the element: previous siblings, then the
  // parent's previous siblings. Stops at another field or a heading, so it
  // never borrows the label of a neighbouring input or the page title.
  const textBefore = el => {
    let node = el;
    for (let depth = 0; depth < 2 && node && node !== document.body; depth++) {
      let sib = node.previousSibling;
      while (sib) {
        let t = '';
        if (sib.nodeType === 3) t = sib.textContent;
        else if (sib.nodeType === 1) {
          if (sib.matches(FORMISH) || sib.querySelector(FORMISH)) return '';
          if (/^H[1-6]$/.test(sib.tagName)) return '';
          t = spaced(sib, false);
        }
        t = clean(t);
        if (t) return t;
        sib = sib.previousSibling;
      }
      node = node.parentElement;
    }
    return '';
  };
  // Text injected through CSS (content: "...") on ::before / ::after.
  const pseudo = (node, which) => {
    if (!node) return '';
    const c = getComputedStyle(node, which).content;
    if (!c || c === 'none' || c === 'normal') return '';
    return clean(c.replace(/^["']|["']$/g, ''));
  };
  const ariaLabelled = el => {   // text of the elements named by aria-labelledby
    const lb = el.getAttribute('aria-labelledby');
    if (!lb) return '';
    const root = el.getRootNode();
    return clean(lb.split(/\\s+/).map(id => root.getElementById(id)
      ? spaced(root.getElementById(id), false) : '').join(' '));
  };
  // Returns [labelText, source] using the most reliable signal first.
  const labelOf = el => {
    if (el.labels && el.labels.length) {
      const t = clean([...el.labels].map(own).join(' '));
      if (t) return [t, 'label-for'];
    }
    const al = clean(el.getAttribute('aria-label'));
    if (al) return [al, 'aria-label'];
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const root = el.getRootNode();
      const t = clean(lb.split(/\\s+/).map(id => (root.getElementById(id) ? spaced(root.getElementById(id), false) : '')).join(' '));
      if (t) return [t, 'aria-labelledby'];
    }
    const w = el.closest('label');
    if (w) { const t = own(w); if (t) return [t, 'wrapping-label']; }
    const ti = clean(el.getAttribute('title'));
    if (ti) return [ti, 'title'];
    const cell = el.closest('td, th');
    if (cell && cell.previousElementSibling) {
      const t = spaced(cell.previousElementSibling, false);
      if (t && t.length < 60) return [t, 'table-cell'];
    }
    const tb = textBefore(el);
    if (tb && tb.length < 60) return [tb, 'preceding-text'];
    for (const [n, w2] of [[el.parentElement, '::before'], [el.previousElementSibling, '::after'],
                           [el, '::before']]) {
      const t = pseudo(n, w2);
      if (t) return [t, 'css-pseudo'];
    }
    return ['', 'none'];
  };
  const skip = ['hidden', 'submit', 'button', 'reset', 'image'];
  const fields = [], radios = {}, combos = new Set();
  const EDITABLE = '[contenteditable]:not([contenteditable=false]), [role=textbox]';
  deepAll(document, 'input, select, textarea, ' + EDITABLE + ', ' + COMBO).forEach((el, i) => {
    const isCombo = el.tagName !== 'SELECT' && el.matches(COMBO);
    const isTextbox = !isCombo && !['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName);
    if (isTextbox && el.querySelector('input, textarea, select')) return;  // just a wrapper
    const host = el.parentElement && el.parentElement.closest(COMBO);
    if (host && combos.has(host)) return;  // inner input of a custom dropdown
    const type = isCombo ? 'combobox' : isTextbox ? 'textbox'
      : el.tagName === 'SELECT' ? 'select'
      : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (skip.includes(type) || el.disabled || el.readOnly) return;
    if (el.getAttribute('aria-disabled') === 'true') return;
    const shown = el.offsetWidth || el.offsetHeight || el.getClientRects().length;
    const labelShown = el.labels && [...el.labels].some(l => l.offsetWidth || l.offsetHeight);
    const toggle = (type === 'checkbox' || type === 'radio') && labelShown;  // styled toggles
    if (!shown && type !== 'file' && !toggle) return;  // file inputs are often hidden
    if (isCombo) combos.add(el);
    el.setAttribute('data-ff-idx', i);
    const [label, source] = labelOf(el);
    const invalid = el.matches(':invalid, [aria-invalid="true"]');
    if (type === 'radio') {
      const key = el.name || ('radio' + i);
      if (!radios[key]) {
        const legend = el.closest('fieldset')?.querySelector('legend')?.innerText || '';
        radios[key] = { kind: 'radio', idx: i, name: el.name, id: '', placeholder: '',
                        autocomplete: '', label: clean(legend), label_source: 'legend',
                        classes: attr(el, 'class'), ctx: ctxOf(el), data: dataOf(el),
                        aria_label: '', labelledby: '', min: '', max: '', maxlength: '',
                        inputmode: '', options: [], invalid: false, message: '' };
        fields.push(radios[key]);
      }
      radios[key].options.push({ idx: i, value: el.value, label });
      radios[key].invalid = radios[key].invalid || invalid;
      return;
    }
    const a = n => el.getAttribute(n) || '';
    const f = { kind: type, idx: i, name: a('name'), id: el.id || '',
                placeholder: a('placeholder'), autocomplete: a('autocomplete'),
                label, label_source: source, options: [],
                classes: attr(el, 'class'), ctx: ctxOf(el), data: dataOf(el),
                inputmode: a('inputmode'),
                aria_label: clean(a('aria-label')), labelledby: ariaLabelled(el),
                maxlength: a('maxlength'), minlength: a('minlength'),
                min: a('min'), max: a('max'), pattern: a('pattern'),
                required: el.required === true || a('aria-required') === 'true',
                invalid, message: el.validationMessage || '' };
    if (type === 'select')
      f.options = [...el.options].map(o => ({ value: o.value, label: o.text }));
    fields.push(f);
  });
  return fields;
}
"""

ERROR_TEXT_JS = """
() => [...document.querySelectorAll(
  '[role=alert], .error, .errors, .invalid-feedback, .field-error, .form-error, .help-block.error')]
  .filter(e => e.offsetWidth || e.offsetHeight)
  .map(e => (e.textContent || '').replace(/\\s+/g, ' ').trim()).filter(Boolean).slice(0, 10)
"""


class CaptchaBlocked(Exception):
    """A CAPTCHA was found and no human solved it."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def norm(s) -> str:
    """Lowercase, no diacritics, only letters and digits."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and short in long_ and len(short) / len(long_) >= 0.5:
        return 0.9  # e.g. "email" in "useremail"
    return SequenceMatcher(None, a, b).ratio()


# CSS classes that carry no meaning (layout / framework utilities / widget types).
STRUCT_STOP = {
    "input", "text", "field", "fields", "form", "control", "group", "wrapper", "wrap",
    "container", "row", "col", "cols", "md", "lg", "sm", "xs", "xl", "btn", "button",
    "required", "error", "valid", "invalid", "is", "has", "mb", "mt", "ml", "mr", "px",
    "py", "pt", "pb", "w", "h", "full", "half", "item", "items", "label", "box", "block",
    "inline", "flex", "grid", "hidden", "visible", "small", "large", "medium", "default",
    "primary", "secondary", "custom", "select", "dropdown", "checkbox", "radio", "number",
    "ui", "js", "no", "the", "and", "of", "for", "class", "focus", "active", "disabled",
    "readonly", "placeholder", "floating", "outline", "border", "rounded", "shadow",
    "style", "type", "element", "component", "widget", "entry", "line", "section", "step",
    "inner", "outer", "content", "body", "main", "base", "root", "second",
    # framework / naming-convention noise (ASP.NET, Django, WooCommerce, Rails, ...)
    "txt", "ddl", "chk", "rb", "tb", "cbo", "lbl", "ctl", "maincontent", "id", "fields",
    "values", "value", "data", "entry", "wpforms", "billing", "shipping", "account",
    "customer", "model", "attributes", "params", "post",
}
# Visible-label vocabulary in other languages (hr, de, fr, es, it, pt, nl, pl).
# Used only against label text, so opaque class/id/name values are never needed.
I18N_LABELS = {
    "firstname": ["ime", "vorname", "prenom", "nombre", "nome", "voornaam", "imie",
                  "primeironome", "nombredepila"],
    "lastname": ["prezime", "nachname", "familienname", "apellido", "apellidos", "cognome",
                 "sobrenome", "achternaam", "nazwisko"],
    "fullname": ["imeiprezime", "vollername", "nomcomplet", "nombrecompleto",
                 "nomecompleto", "nomeecognome", "volledigenaam", "imieinazwisko"],
    "email": ["eposta", "epost", "mail", "courriel", "correo", "correoelectronico",
              "emailadresse", "indirizzoemail", "adresaeposte", "emailadres"],
    "phone": ["telefon", "mobitel", "mobilni", "handy", "telefonnummer", "mobiltelefon",
              "telephone", "portable", "telefono", "movil", "celular", "telefone",
              "telefoon", "komorka", "brojmobitela", "brojtelefona"],
    "password": ["lozinka", "sifra", "passwort", "kennwort", "motdepasse", "contrasena",
                 "senha", "wachtwoord", "haslo"],
    "username": ["korisnickoime", "korisnik", "benutzername", "nutzername", "identifiant",
                 "usuario", "nomeutente", "gebruikersnaam", "uzytkownik"],
    "birthdate": ["datumrodenja", "rodenje", "geburtsdatum", "datedenaissance",
                  "fechadenacimiento", "datadinascita", "datadenascimento",
                  "geboortedatum", "dataurodzenia"],
    "gender": ["spol", "geschlecht", "sexe", "genre", "sexo", "genero", "sesso",
               "geslacht", "plec"],
    "city": ["grad", "mjesto", "stadt", "ort", "ville", "ciudad", "citta", "cidade",
             "stad", "miasto"],
    "country": ["drzava", "zemlja", "land", "pays", "pais", "paese", "kraj"],
    "address": ["adresa", "ulica", "adresse", "strasse", "direccion", "calle", "indirizzo",
                "via", "endereco", "straat"],
    "message": ["poruka", "komentar", "napomena", "nachricht", "kommentar", "mensaje",
                "comentario", "messaggio", "mensagem", "bericht", "wiadomosc"],
    "company": ["tvrtka", "firma", "poduzece", "unternehmen", "entreprise", "empresa",
                "azienda"],
}
EXACT_ONLY = {"name"}  # too generic to match as a part of another name (username, ...)
LABEL_STOP = {"the", "your", "enter", "please", "select", "choose", "or", "and", "ili",
              "odaberite", "odaberi", "unesite", "und", "oder", "ou", "o", "de", "la", "el"}
HASHED = re.compile(r"^(css|sc|jsx|svelte|ember|_)?-?[a-z]{0,3}[0-9a-f]{5,}$")


def _ascii(s) -> str:
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()


def tokens(raw, stop=STRUCT_STOP) -> list[str]:
    """Split camelCase / snake_case / kebab-case / spaces into lowercase words."""
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", _ascii(raw))
    out = []
    for w in re.findall(r"[A-Za-z]+|\d+", t):
        w = w.lower()
        if len(w) < 2 or w in stop or w.isdigit() or HASHED.match(w):
            continue
        out.append(w)
    return out


def names_for(key: str, i18n: bool = False) -> list[str]:
    names = [norm(key)] + ALIASES.get(norm(key), [])
    if i18n:
        names += I18N_LABELS.get(norm(key), [])
    return names


def struct_raws(f: dict) -> list[str]:
    """Language independent strings: name, id, classes, data-*, wrapper classes, hints."""
    ac = f.get("autocomplete", "").split()
    ph = (f.get("placeholder") or "").strip()
    shape = ("email" if re.fullmatch(r"\S+@\S+", ph)
             else "phone" if re.fullmatch(r"\+?[\d\s().\-]{7,}", ph) else None)
    hints = [AUTOCOMPLETE_HINTS.get(ac[-1]) if ac else None, KIND_HINTS.get(f["kind"]),
             {"email": "email", "tel": "phone", "numeric": None}.get(f.get("inputmode", "")),
             shape]
    raw = [f.get("name"), f.get("id"), f.get("classes"), f.get("data"),
           f.get("ctx"), f.get("placeholder"), *hints]
    return [r for r in raw if r]


def label_raws(f: dict) -> list[str]:
    return [r for r in (f.get("label"),) if r]


PHONE_HINTS_IN_LABEL = ("telefon", "telephone", "mobitel", "mobilni", "mobiltel",
                        "phone", "tel", "cell", "mobile", "handy", "broj", "number",
                        "msisdn")


def _name_score(raws: list[str], names: list[str], stop=STRUCT_STOP) -> float:
    best = 0.0
    for raw in raws:
        whole = norm(raw)
        toks = tokens(raw, stop)
        for n in names:
            if n in EXACT_ONLY:
                best = max(best, 1.0 if whole == n else 0.0)
                continue

            s = similarity(whole, n)
            for t in toks:
                s = max(s, 0.9 * similarity(t, n))
            if len(n) >= 5 and len(whole) > len(n) and n in whole:
                s = max(s, min(0.95, 0.8 + 0.01 * len(n)))

            # Penaliziraj email ako label očito traži telefon/mobitel.
            # Primjer: "Broj mobitela ili adresa e-pošte" — email inače pobijedi
            # telefon za 0.02, a ovo ga spusti dovoljno da phone pobijedi.
            if n == "email" and any(h in whole for h in PHONE_HINTS_IN_LABEL):
                s *= 0.5

            best = max(best, s)
    return best


# Signals, most reliable first (weight = how far a match on that tier is trusted).
TIER_WEIGHTS = [
    ("autocomplete", 1.00), ("name", 0.98), ("id", 0.96), ("type", 0.94),
    ("aria-label", 0.91), ("aria-labelledby", 0.88), ("placeholder", 0.85),
    ("class", 0.83), ("text", 0.80),
]
INPUTMODE_HINTS = {"email": "email", "tel": "phone"}
LANGUAGE_TIERS = {"aria-label", "aria-labelledby", "placeholder", "text"}


def tier_signals(f: dict) -> dict[str, list[str]]:
    """The raw strings of every tier for one field."""
    ac = f.get("autocomplete", "").split()
    ph = (f.get("placeholder") or "").strip()
    shape = ("email" if re.fullmatch(r"\S+@\S+", ph)
             else "phone" if re.fullmatch(r"\+?[\d\s().\-]{7,}", ph) else None)
    src = f.get("label_source", "")
    aria = [f.get("aria_label")] + ([f["label"]] if src == "title" else [])
    text = [] if src in ("aria-label", "aria-labelledby", "title") else [f.get("label")]
    sig = {
        "autocomplete": [AUTOCOMPLETE_HINTS.get(ac[-1]) if ac else None],
        "name": [f.get("name")],
        "id": [f.get("id")],
        "type": [KIND_HINTS.get(f["kind"]), INPUTMODE_HINTS.get(f.get("inputmode", ""))],
        "aria-label": aria,
        "aria-labelledby": [f.get("labelledby")],
        "placeholder": [ph, shape],
        "class": [f.get("classes"), f.get("data"), f.get("ctx")],
        "text": text,
    }
    return {k: [r for r in v if r] for k, v in sig.items()}


def score_field_key(f: dict, key: str):
    """(score, tier). A match is worth the weight of the tier it was found in."""
    sig = tier_signals(f)
    best, best_tier = 0.0, ""
    for tier, weight in TIER_WEIGHTS:
        raws = sig.get(tier)
        if not raws:
            continue
        stop = LABEL_STOP if tier in LANGUAGE_TIERS else STRUCT_STOP
        raw_score = _name_score(raws, names_for(key, i18n=tier in LANGUAGE_TIERS), stop)
        if raw_score * weight > best:
            best, best_tier = raw_score * weight, tier
    return best, best_tier


def compatible(f: dict, value) -> bool:
    """Reject values that cannot go into this input type (e.g. a street in an email box)."""
    v, k = str(value).strip(), f["kind"]
    # e-mail: must contain @ and a dot after it (so a phone number never lands here)
    if k == "email":
        return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v))
    # phone: only digits, spaces, () + - and a leading +; no letters, no @
    if k == "tel":
        if "@" in v or re.search(r"[A-Za-z]", v):
            return False
        return bool(re.fullmatch(r"\+?[0-9][0-9\s().\-]{4,}", v))
    if k in ("number", "range"):
        return bool(re.fullmatch(r"-?\d+([.,]\d+)?", v))
    if k == "date":
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v))
    return True


def key_kind_ok(f: dict, key: str) -> bool:
    """Guard against a data key landing in a field of an incompatible kind."""
    k, nk = f["kind"], norm(key)
    label = norm(f.get("label", ""))

    # Ako label traži telefon/mobitel → email ne može unutra
    if nk == "email" and any(h in label for h in PHONE_HINTS_IN_LABEL):
        return False

    if k == "email" and nk in ("phone", "password", "username", "address", "message"):
        return False
    if k == "tel" and nk in ("email", "password", "address", "message", "username"):
        return False
    if k == "password" and nk in ("email", "phone", "address", "message", "username",
                                  "firstname", "lastname", "fullname", "city", "country"):
        return False
    if k in ("number", "range") and nk in ("email", "phone", "password", "address",
                                           "message", "username", "firstname", "lastname",
                                           "fullname"):
        return False
    return True


def assign_keys(fields: list[dict], record: dict, skip: set) -> dict:
    """Global assignment {field index: (key, score, source)}; strongest pairs first.

    Pass 1 uses every key once. Pass 2 lets an already used key fill further fields
    only on a near-exact match (e.g. a repeated e-mail confirmation box).
    """
    cands = []
    for i, f in enumerate(fields):
        if i in skip:
            continue
        for key, value in record.items():
            if not key_kind_ok(f, key):
                continue
            s, src = score_field_key(f, key)
            if s >= THRESHOLD and compatible(f, value):
                cands.append((s, i, key, src))
    cands.sort(key=lambda c: (-c[0], c[1]))
    out, used = {}, set()
    for s, i, key, src in cands:
        if i not in out and key not in used:
            out[i] = (key, s, src)
            used.add(key)
    for s, i, key, src in cands:
        if i not in out and s >= 0.8:
            out[i] = (key, s, src)
    return out


# ---- birth date split over day / month / year selects -----------------------
PART_WORDS = {  # English + Croatian (+ a little German) words for the three parts
    "day": {"day", "dd", "dy", "dan", "dani", "tag"},
    "month": {"month", "mm", "mon", "mjesec", "mjeseca", "mj", "monat"},
    "year": {"year", "yyyy", "yy", "godina", "godinu", "godine", "god", "gggg", "jahr"},
}


CARD_WORDS = {"exp", "expiry", "expiration", "expires", "expire", "card", "cc", "cvv",
              "cvc", "valid", "validity"}


def _parts_or_words(raws, words) -> bool:
    return any(t in words for raw in raws if raw for t in tokens(raw, stop=set()))


def _parts_in(raws) -> set[str]:
    found = set()
    for raw in raws:
        for t in tokens(raw, stop=set()):
            for part, words in PART_WORDS.items():
                if t in words:
                    found.add(part)
    return found


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def date_part(f: dict):
    """(part, strong) for day / month / year fields of any kind, else None.

    strong = decided by words in the HTML; weak = guessed from numeric ranges, which
    is only trusted when at least two parts of a date are present on the page.
    """
    if f["kind"] not in ("select", "text", "number", "tel", "combobox"):
        return None
    own = [f.get(k) for k in ("name", "id", "aria_label", "labelledby", "placeholder",
                              "classes", "data", "label")]
    if _parts_or_words(own + [f.get("ctx")], CARD_WORDS):  # card expiry etc., not a birth date
        return None
    parts = _parts_in([r for r in own if r])
    if len(parts) == 1:
        return parts.pop(), True
    if not parts:
        ctx = _parts_in([f["ctx"]] if f.get("ctx") else [])
        if len(ctx) == 1:
            return ctx.pop(), True
    if f["kind"] in ("number", "text", "tel"):
        mn, mx = _int(f.get("min")), _int(f.get("max"))
        if mn == 1 and mx == 31:
            return "day", False
        if mn == 1 and mx == 12:
            return "month", False
        if mn is not None and mn >= 1900:
            return "year", False
    if f["kind"] == "select":
        texts = [(o["value"] or o["label"]).strip() for o in f["options"]]
        nums = [int(t) for t in texts if re.fullmatch(r"\d{1,4}", t) and int(t) > 0]
        if len(nums) >= 28 and min(nums) == 1 and 28 <= max(nums) <= 31:
            return "day", False
        if len(nums) >= 20 and min(nums) >= 1900:
            return "year", False
        if sorted(nums) == list(range(1, 13)):
            return "month", False
    return None


def detect_date_parts(fields: list[dict], date_src) -> dict:
    """{field index: part}. A date is split only if the page really has at least two of
    its parts (day / month / year); a single date field is always filled as a whole."""
    if not date_src:
        return {}
    cand = {n: r for n, f in enumerate(fields) if (r := date_part(f))}
    present = {part for part, _ in cand.values()}
    if len(present) < 2:
        return {}
    return {n: part for n, (part, _strong) in cand.items()}


def date_value(record: dict):
    """(key, (year, month, day)) from the first date-like value; birth-ish keys first."""
    found = []
    for key, v in record.items():
        v = str(v).strip()
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
        ymd = (int(m[1]), int(m[2]), int(m[3])) if m else None
        if not ymd and (m := re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", v)):
            ymd = (int(m[3]), int(m[2]), int(m[1]))
        if ymd:
            birthish = bool(re.search(r"birth|dob|bday|date|datum", norm(key)))
            found.append((not birthish, key, ymd))
    found.sort(key=lambda t: t[0])
    return (found[0][1], found[0][2]) if found else None





def fill_date_part(f: dict, part: str, ymd) -> str:
    want = {"year": ymd[0], "month": ymd[1], "day": ymd[2]}[part]

    MONTH_NAMES_FOR = {
        1: ["January", "Jan", "Januar", "Siječanj", "Janvier"],
        2: ["February", "Feb", "Februar", "Veljača", "Février"],
        3: ["March", "Mar", "März", "Ožujak", "Mars"],
        4: ["April", "Apr", "Travanj", "Avril"],
        5: ["May", "Svibanj", "Mai"],
        6: ["June", "Jun", "Juni", "Lipanj", "Juin"],
        7: ["July", "Jul", "Juli", "Srpanj", "Juillet"],
        8: ["August", "Aug", "Kolovoz", "Août"],
        9: ["September", "Sep", "Sept", "Rujan", "Septembre"],
        10: ["October", "Oct", "Oktober", "Listopad", "Octobre"],
        11: ["November", "Nov", "Studeni", "Novembre"],
        12: ["December", "Dec", "Dezember", "Prosinac", "Décembre"],
    }

    # --- combobox (custom dropdown) ---------------------------------------
    if f["kind"] == "combobox":
        if part == "month":
            # Probaj svako ime mjeseca dok jedno ne prođe
            for name in MONTH_NAMES_FOR.get(want, []):
                result = fill_field(f, name)
                if "no option" not in result:
                    return result.replace("combobox", f"combobox({part})", 1)
            # Ako ni jedno ime nije prošlo, probaj broj kao string
            result = fill_field(f, str(want))
            return result.replace("combobox", f"combobox({part})", 1)
        # Dan i godina: broj
        return fill_field(f, str(want)).replace("combobox", f"combobox({part})", 1)

    # --- text / number ----------------------------------------------------
    if f["kind"] != "select":
        ph = (f.get("placeholder") or "").lower()
        ml = _int(f.get("maxlength"))
        if part == "year":
            two = ml == 2 or (("yy" in ph or "gg" in ph) and "yyyy" not in ph
                              and "gggg" not in ph)
            text = f"{want % 100:02d}" if two else str(want)
        else:
            text = f"{want:02d}" if (ml == 2 or ph in ("dd", "mm", "dd.", "mm.")) else str(want)
        return fill_field(f, text).replace(" -> ", f"({part}) -> ", 1)

    # --- select -----------------------------------------------------------
    opts = [o for o in f["options"] if (o["value"] or "").strip() != ""]
    chosen = None

    for o in opts:
        for t in (o["value"], o["label"]):
            t = (t or "").strip()
            if re.fullmatch(r"0*\d{1,4}", t) and int(t) == want:
                chosen = o
                break
        if chosen:
            break

    # 2) label je ime mjeseca
    if chosen is None and part == "month":
        want_names = {norm(n) for n in MONTH_NAMES_FOR.get(want, [])}
        for o in opts:
            if norm(o["label"]) in want_names:
                chosen = o
                break

    if chosen is None and part == "month" and len(opts) == 12:
        chosen = opts[want - 1]

    if chosen is None and part == "month" and len(opts) == 13:
        chosen = opts[want]

    if chosen is None:
        return f"no option for {part}={want}"

    field_locator(f).select_option(value=chosen["value"])
    return f"select({part}) -> {chosen['label'] or chosen['value']}"


SYN_GROUPS = [
    {"female", "f", "woman", "zensko", "zena", "zenski", "weiblich", "femme", "mujer",
     "donna", "feminino"},
    {"male", "m", "man", "musko", "muski", "maennlich", "mannlich", "homme", "hombre",
     "uomo", "masculino"},
    {"yes", "y", "true", "da", "ja", "oui", "si", "sim"},
    {"no", "n", "false", "ne", "nein", "non", "nao"},
]


def pick_option(options: list[dict], value) -> dict | None:
    """Choose an option (select/radio/combobox) by value or label (with synonyms)."""
    target = norm(value)
    targets = {target}
    for g in SYN_GROUPS:
        if target in g:
            targets |= g
    scored = [(max(similarity(t, norm(o["value"])) for t in targets) if o["value"] else 0.0,
               o) for o in options]
    scored = [(max(sv, max(similarity(t, norm(o["label"])) for t in targets)), o)
              for sv, o in scored]
    score, opt = max(scored, key=lambda t: t[0], default=(0, None))
    return opt if score >= THRESHOLD else None


def adapt_value(f: dict, value) -> str:
    """Fit a value to the field: date placeholder formats and maxlength."""
    s = str(value)
    ph = f.get("placeholder", "").lower()
    if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) and f["kind"] in ("text", "search")
            and re.search(r"[dmy]", ph)):
        y, m, d = s.split("-")
        order = []
        for ch in re.findall(r"[dmy]", ph):
            if ch not in order:
                order.append(ch)
        if len(order) == 3:
            sep = next((c for c in ph if not c.isalnum()), "/")
            parts = {"d": d, "m": m, "y": y if "yyyy" in ph else y[2:]}
            s = sep.join(parts[c] for c in order)
    try:
        mx = int(f.get("maxlength") or -1)
    except ValueError:
        mx = -1
    return s[:mx] if mx >= 0 else s


def discover(page: Page) -> list[dict]:
    """Fields from the main document and every iframe (each carries its frame)."""
    fields = []
    for fr in page.frames:
        try:
            found = fr.evaluate(DISCOVER_JS)
        except Exception:  # frame detached / not reachable
            continue
        for f in found:
            f["frame"] = fr
            f["in_iframe"] = fr != page.main_frame
        fields.extend(found)
    return fields


def field_locator(f: dict):
    return f["frame"].locator(f'[data-ff-idx="{f["idx"]}"]')


def fill_combobox(fr: Frame, loc, value) -> str:
    """Custom dropdown: click, (type if it is an input), then click the option."""
    loc.click()
    is_input = loc.evaluate("e => e.tagName") == "INPUT"
    if is_input:
        loc.fill(str(value))
    fr.page.wait_for_timeout(250)
    visible = []
    for scope in (fr, fr.page.main_frame):  # the popup may live in a portal
        visible = [o for o in scope.locator("[role=option], [role=listbox] li").all()
                   if o.is_visible()]
        if visible:
            break
    cands = [{"idx": i, "label": o.inner_text().strip(), "value": ""}
             for i, o in enumerate(visible)]
    opt = pick_option(cands, value)
    if opt:
        visible[opt["idx"]].click()
        return f"combobox -> {opt['label']}"
    if is_input:
        loc.press("Enter")
        return f"combobox (typed + Enter) -> {value}"
    return f"no option for '{value}'"


def _set_checked(loc, desired: bool = True) -> None:
    """Check/uncheck, also for visually hidden (styled) checkboxes and radios."""
    try:
        loc.set_checked(desired, timeout=2000)
        return
    except Exception:
        pass
    try:
        loc.set_checked(desired, force=True, timeout=2000)
    except Exception:
        loc.evaluate("(e, v) => { if (e.checked !== v) e.click(); }", desired)


def fill_field(f: dict, value) -> str:
    fr: Frame = f["frame"]
    kind = f["kind"]
    if kind == "radio":
        opt = pick_option(f["options"], value)
        if not opt:
            return f"no option for '{value}'"
        _set_checked(fr.locator(f'[data-ff-idx="{opt["idx"]}"]'))
        return f"radio -> {opt['label'] or opt['value']}"
    loc = field_locator(f)
    if kind == "file":
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = (BASE / path) if (BASE / path).exists() else path.resolve()
        if not path.exists():
            return f"file not found: {value}"
        loc.set_input_files(str(path))
        return f"file -> {path.name}"
    if kind == "combobox":
        return fill_combobox(fr, loc, value)
    if kind == "checkbox":
        truthy = str(value).strip().lower() in ("1", "true", "yes", "y", "x", "on")
        _set_checked(loc, truthy)
        return f"checkbox -> {truthy}"
    if kind == "select":
        opt = pick_option(f["options"], value)
        if not opt:
            return f"no option for '{value}'"
        loc.select_option(value=opt["value"])
        return f"select -> {opt['label']}"
    text = adapt_value(f, value)
    try:
        human_type(loc, text)
    except Exception:
        if kind != "textbox":
            raise
        loc.click()
        fr.page.keyboard.type(text)
    if kind == "password":  # never write secrets to the log or report.json
        return f"password -> filled ({len(text)} characters)"
    return f"{kind} -> {text}"

def human_move_mouse(page: Page, x: int, y: int, steps: int = 25) -> None:
    """Move the mouse along a Bézier curve, clamped to the viewport."""
    vp = page.viewport_size or {"width": 1920, "height": 1080}
    w, h = vp["width"], vp["height"]

    # Kontrolna točka — ali unutar viewporta
    cx = max(0, min(w, x + random.randint(-100, 100)))
    cy = max(0, min(h, y + random.randint(-100, 100)))

    for i in range(steps + 1):
        t = i / steps
        px = int((1 - t) ** 2 * 0 + 2 * (1 - t) * t * cx + t ** 2 * x)
        py = int((1 - t) ** 2 * 0 + 2 * (1 - t) * t * cy + t ** 2 * y)
        # Ograniči na viewport da Playwright ne scrolla
        px = max(0, min(w - 1, px))
        py = max(0, min(h - 1, py))
        page.mouse.move(px, py)
        page.wait_for_timeout(random.randint(5, 20))

def human_click(loc) -> None:
    """Click with a small mouse movement toward the element first."""
    box = loc.bounding_box()
    if box:
        target_x = box["x"] + box["width"] / 2
        target_y = box["y"] + box["height"] / 2
        page = loc.page
        human_move_mouse(page, int(target_x), int(target_y))
        page.wait_for_timeout(random.uniform(50, 150))
    loc.click()

def iter_buttons(page: Page):
    """Yield (locator, normalized_text, type) for visible, enabled buttons."""
    for fr in page.frames:
        try:
            buttons = fr.locator(
                "button, input[type=button], input[type=submit], "
                "[role=button]:not([aria-haspopup])").all()
        except Exception:
            continue
        for btn in buttons:
            if not btn.is_visible() or not btn.is_enabled():
                continue
            text = norm(btn.inner_text() or btn.get_attribute("value") or "")
            yield btn, text, btn.evaluate("e => e.type")


def is_next(text: str) -> bool:
    return text.startswith(tuple(norm(w) for w in CFG.next_words))


def find_next(page: Page):
    """Find a 'Next / Continue' button."""
    if CFG.next_selector:
        for fr in page.frames:
            for el in fr.locator(CFG.next_selector).all():
                if el.is_visible() and el.is_enabled():
                    return el
    for btn, text, _ in iter_buttons(page):
        if is_next(text):
            return btn
    return None


def find_submit(page: Page):
    """Find the submit button: custom selector > type=submit > submit-like text."""
    if CFG.submit_selector:
        for fr in page.frames:
            for el in fr.locator(CFG.submit_selector).all():
                if el.is_visible() and el.is_enabled():
                    return el
    words = tuple(norm(w) for w in CFG.submit_words)
    by_text = None
    for btn, text, typ in iter_buttons(page):
        if is_next(text):
            continue
        if typ == "submit":
            return btn
        if by_text is None and text.startswith(words):
            by_text = btn
    return by_text


def captcha_present(page: Page) -> bool:
    """True only if a CAPTCHA is VISIBLE (not just present in the DOM)."""
    for fr in page.frames:
        try:
            locs = fr.locator(CAPTCHA_SELECTOR).all()
            for loc in locs:
                try:
                    if loc.is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def captcha_solved(page: Page) -> bool:
    """True if a CAPTCHA token is present, or if no VISIBLE CAPTCHA remains."""
    # 1) Provjeri je li token upisan u hidden polje
    for fr in page.frames:
        try:
            if fr.evaluate(
                "() => [...document.querySelectorAll("
                "'textarea[name$=response], input[name$=response], "
                "textarea[name=g-recaptcha-response], "
                "textarea[name=h-captcha-response], "
                "input[name=cf-turnstile-response]')]"
                ".some(e => e.value && e.value.length > 20)"):
                return True
        except Exception:
            continue

    # 2) Ako nema VIDLJIVE CAPTCHA-e, smatraj je riješenom
    return not captcha_present(page)


def wait_for_human_captcha(page: Page) -> None:
    """CAPTCHAs are NOT bypassed: the script stops and waits for a human."""
    if not captcha_present(page):
        return
    if CFG.headless:
        raise CaptchaBlocked("CAPTCHA found; run without headless to solve it manually.")
    page.bring_to_front()
    if CFG.interactive:
        input("  CAPTCHA found. Solve it manually in the browser, then press Enter...")
        return
    log(f"  CAPTCHA found. Solve it in the browser window "
        f"(waiting up to {CFG.captcha_timeout}s)...")
    waited = 0
    while waited < CFG.captcha_timeout:
        if captcha_solved(page):
            log("  CAPTCHA solved, continuing.")
            return
        page.wait_for_timeout(1000)
        waited += 1
    raise CaptchaBlocked("CAPTCHA was not solved in time.")


# Well-known consent-management platforms (OneTrust, Cookiebot, Didomi, ...).
CMP_SELECTORS = {
    "accept": [
        "#onetrust-accept-btn-handler",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "#CybotCookiebotDialogBodyButtonAccept",
        "#didomi-notice-agree-button",
        ".qc-cmp2-summary-buttons button[mode=primary]",
        "[data-testid=uc-accept-all-button]",
        ".cmplz-accept", ".cc-allow", ".cc-btn.cc-dismiss", ".osano-cm-accept-all",
        "#truste-consent-button", ".fc-cta-consent",
    ],
    "reject": [
        "#onetrust-reject-all-handler",
        "#CybotCookiebotDialogBodyButtonDecline",
        "#didomi-notice-disagree-button",
        "[data-testid=uc-deny-all-button]",
        ".cmplz-deny", ".osano-cm-deny", ".cc-deny",
    ],
}
# Generic banners: only buttons inside containers that look like consent dialogs.
CONSENT_CONTAINERS = (
    ":is([id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], "
    "[id*=gdpr i], [class*=gdpr i], [aria-label*=cookie i], [aria-label*=consent i], "
    "[role=dialog], [role=alertdialog])")
CONSENT_BUTTONS = ":is(button, a, [role=button], input[type=button], input[type=submit])"
ACCEPT_STARTS = ("accept", "agree", "allow", "iagree", "iaccept", "gotit", "understand",
                 "prihvati", "prihvacam", "slazemse", "uredu", "razumijem", "dopusti")
NOT_ACCEPT = ("reject", "decline", "deny", "refuse", "necessary", "essential", "only",
              "manage", "setting", "preference", "custom", "odbij", "postavk", "samo")
REJECT_PARTS = ("reject", "decline", "deny", "refuse", "necessaryonly", "onlynecessary",
                "essentialonly", "onlyessential", "odbij", "odbi")


def _cookie_button_text(btn) -> str:
    return norm(btn.inner_text() or btn.get_attribute("value") or "")


def _click_cookie_button(page: Page, mode: str) -> str | None:
    for sel in CMP_SELECTORS[mode]:
        for fr in page.frames:
            try:
                loc = fr.locator(sel).first
                if loc.count() and loc.is_visible():
                    label = (loc.inner_text() or sel).strip()[:40]
                    loc.click(timeout=2000)
                    return label
            except Exception:
                continue
    for fr in page.frames:
        try:
            buttons = fr.locator(f"{CONSENT_CONTAINERS} {CONSENT_BUTTONS}").all()
        except Exception:
            continue
        for btn in buttons:
            try:
                if not btn.is_visible():
                    continue
                text = _cookie_button_text(btn)
                if not text or len(text) > 40:
                    continue
                if mode == "accept":
                    hit = (text == "ok" or text.startswith(ACCEPT_STARTS)) \
                        and not any(w in text for w in NOT_ACCEPT)
                else:
                    hit = any(w in text for w in REJECT_PARTS)
                if hit:
                    label = (btn.inner_text() or btn.get_attribute("value") or "").strip()
                    btn.click(timeout=2000)
                    return label[:40]
            except Exception:
                continue
    return None


def handle_cookies(page: Page) -> None:
    """Accept or reject a cookie banner if one shows up (banners often appear late)."""
    mode = CFG.cookies
    if mode == "ignore":
        return
    for attempt in range(4):
        clicked = _click_cookie_button(page, mode)
        if clicked:
            done = "accepted" if mode == "accept" else "rejected"
            log(f"  Cookie banner {done} via {clicked!r}")
            page.wait_for_timeout(300)
            return
        if attempt < 3:
            page.wait_for_timeout(500)


def highlight(f: dict, color: str) -> None:
    """Outline the field and scroll it into view so a human can follow along."""
    if CFG.headless or not CFG.highlight:
        return
    try:
        field_locator(f).evaluate(
            "(e, c) => { e.scrollIntoView({block: 'center'});"
            " e.style.outline = '3px solid ' + c; e.style.outlineOffset = '2px'; }",
            color)
    except Exception:
        pass


def composite_values(record: dict) -> tuple[dict, dict]:
    """Virtual keys built from real ones, e.g. full_name = first_name + last_name."""
    first = next((k for k in record if norm(k) in ("firstname", "givenname", "fname")), None)
    last = next((k for k in record if norm(k) in ("lastname", "surname", "lname")), None)
    has_full = any(norm(k) in ("fullname", "name") for k in record)
    if first and last and not has_full:
        return ({"full_name": f"{record[first]} {record[last]}"},
                {"full_name": (first, last)})
    return {}, {}


def apply_map(fields: list[dict], record: dict) -> dict:
    """Manual overrides: {css selector: data key | "=literal value"} -> {field index: (key, value)}."""
    out = {}
    for sel, target in CFG.field_map.items():
        literal = isinstance(target, str) and target.startswith("=")
        if not literal and target not in record:
            continue
        value = target[1:] if literal else record[target]
        for fr in {f["frame"] for f in fields}:
            try:
                idxs = fr.evaluate(
                    "sel => [...document.querySelectorAll(sel)]"
                    ".map(e => e.getAttribute('data-ff-idx'))", sel)
            except Exception:
                continue
            for n, f in enumerate(fields):
                if f["frame"] == fr and str(f["idx"]) in [str(i) for i in idxs if i]:
                    out[n] = ("<literal>" if literal else target, value)
    return out


def top_candidates(f: dict, keys: list[str], n: int = 3) -> list[dict]:
    rows = []
    for k in keys:
        sc, src = score_field_key(f, k)
        rows.append({"key": k, "score": round(sc, 2), "source": src})
    rows.sort(key=lambda r: -r["score"])
    return rows[:n]


def describe(f: dict) -> dict:
    return {"kind": f["kind"], "name": f["name"], "id": f["id"], "class": f.get("classes", ""),
            "data": f.get("data", ""), "ctx": f.get("ctx", ""), "label": f["label"],
            "label_source": f["label_source"], "placeholder": f["placeholder"],
            "autocomplete": f.get("autocomplete", ""), "options": len(f.get("options", [])),
            "required": f.get("required", False), "in_iframe": f.get("in_iframe", False)}


def inspect_page(page: Page, record: dict) -> list[dict]:
    """List every field with the signals read from the page and its best data keys."""
    fields = discover(page)
    record = {**record, **composite_values(record)[0]}
    keys = list(record.keys()) or list(ALIASES.keys())
    log(f"  Fields found: {len(fields)}")
    out = []
    for n, f in enumerate(fields, 1):
        info = describe(f)
        info["top"] = top_candidates(f, keys)
        dp = date_part(f)
        info["part"] = dp[0] if dp else None
        out.append(info)
        best = info["top"][0] if info["top"] else None
        verdict = (f"{best['key']!r} ({best['score']:.2f} {best['source']})"
                   if best and best["score"] >= THRESHOLD else "no match")
        log(f"  {n:2}. <{info['kind']}> name={info['name']!r} id={info['id']!r} "
            f"label={info['label']!r} [{info['label_source']}]")
        log(f"      class={info['class'][:50]!r} ctx={info['ctx'][:40]!r} "
            f"data={info['data'][:40]!r}")
        log(f"      -> {verdict}" + (f"  date-part={info['part']}" if info["part"] else ""))
    return out


def fill_step(page: Page, record: dict, used: set, entries: list, gen: DataGen):
    fields = discover(page)
    last = None
    log(f"  Fields found: {len(fields)}")
    extras, comp_src = composite_values(record)
    record = {**record, **extras}
    date_src = date_value(record)
    parts = detect_date_parts(fields, date_src)
    fixed = apply_map(fields, record)
    assigned = assign_keys(fields, record, skip=set(parts) | set(fixed))
    if CFG.debug:
        for n, f in enumerate(fields):
            d = describe(f)
            log(f"    . {n}: {d['kind']} name={d['name']!r} id={d['id']!r} "
                f"class={d['class'][:40]!r} label={d['label']!r}[{d['label_source']}]")
            log(f"      top: {top_candidates(f, list(record.keys()))}")
    for n, f in enumerate(fields):
        title = f["label"] or f["name"] or f["id"] or f["placeholder"] or f["kind"]
        ident = f["name"] or f["id"] or f["kind"]
        where = " [iframe]" if f["in_iframe"] else ""
        if n in parts:
            key = date_src[0]
            used.add(key)
            highlight(f, "#ff9800")
            result = fill_date_part(f, parts[n], date_src[1])
            highlight(f, "#4caf50")
            score, src = 1.0, "date-part"
            last = f
        elif n in fixed:
            key, value = fixed[n]
            used.add(key)
            highlight(f, "#ff9800")
            result = fill_field(f, value)
            highlight(f, "#4caf50")
            score, src = 1.0, "map"
            last = f
        elif n in assigned:
            key, score, how = assigned[n]
            used.add(key)
            used.update(comp_src.get(key, ()))
            highlight(f, "#ff9800")
            result = fill_field(f, record[key])
            highlight(f, "#4caf50")
            src = f"{how} {score:.2f}"
            last = f
        elif CFG.fake_missing and (value := gen.generate(f)) is not None:
            highlight(f, "#ff9800")
            result = fill_field(f, value)
            highlight(f, "#4caf50")
            key, score, src = "<generated>", 0.0, "faker"
            last = f
        else:
            log(f"  [ - ] {title[:22]!r:24} <{ident[:20]}> no data{where}")
            continue
        entries.append({"field": title, "label_source": f["label_source"], "key": key,
                        "score": round(score, 2), "source": src, "result": result})
        log(f"  [ + ] {title[:22]!r:24} <{ident[:20]}> <- {key!r} ({src}): "
            f"{result}{where}")
    return fields, last

def is_confirmation_page(page: Page) -> bool:
    """True if the current page asks for a confirmation code (Facebook, etc.)."""
    try:
        url = page.url.lower()
        if "confirmemail" in url or "confirm_email" in url:
            return True
    except Exception:
        pass

    try:
        content = page.content().lower()
    except Exception:
        return False

    # Facebook i slični: tekst + polje za kod
    keywords = (
        "enter the confirmation code",
        "enter the code",
        "confirmation code",
        "verify your email",
        "check your inbox",
        "we've sent",
        "unesite kod",
        "potvrdite svoju e-poštu",
        "verifikacijski kod",
    )
    if not any(k in content for k in keywords):
        return False

    # Provjeri postoji li polje za unos koda
    try:
        for fr in page.frames:
            fields = fr.evaluate(DISCOVER_JS)
            for f in fields:
                label = (f.get("label") or "").lower()
                if "code" in label or "kod" in label or "confirmation" in label:
                    return True
    except Exception:
        pass

    return False

def submit_form(page: Page, last_field) -> dict:
    """Click submit; if CAPTCHA appears, pause for the human to solve it."""
    method = None
    btn = find_submit(page)
    url_before = page.url
    content_before = ""
    try:
        content_before = page.content()
    except Exception:
        pass

    if btn:
        label = (btn.inner_text() or btn.get_attribute("value") or "").strip()
        btn.click()
        method = f"clicked button {label!r}"
    elif last_field is not None:
        loc = field_locator(last_field)
        if loc.evaluate("e => e.form ? (e.form.requestSubmit(), true) : false"):
            method = "form.requestSubmit()"
        else:
            loc.press("Enter")
            method = "pressed Enter in last field"

    if method is None:
        log("  No submit button found.")
        return {"submitted": False, "accepted": False, "method": None,
                "invalid": [], "page_errors": []}

    # --- čekaj 3s da se pojavi CAPTCHA ili loading ------------------------
    log("  Waiting for server response...")
    page.wait_for_timeout(3000)

    # --- ako se pojavila CAPTCHA, PAUZIRAJ i čekaj čovjeka ----------------
    if captcha_present(page):
        log("  ⚠ CAPTCHA detected. SOLVE IT IN THE BROWSER WINDOW.")
        log("    (Script will wait up to 10 minutes.)")
        page.bring_to_front()

        waited = 0
        while not captcha_solved(page):
            page.wait_for_timeout(2000)
            waited += 2
            if waited % 30 == 0:
                log(f"    ...still waiting ({waited}s)")
            if waited >= 600:
                log("  ✗ CAPTCHA not solved in 10 minutes. Giving up.")
                break

        if captcha_solved(page):
            log("  ✓ CAPTCHA solved. Continuing...")
            page.wait_for_timeout(2000)

    # --- nastavi s analizom rezultata -------------------------------------
    try:
        page.wait_for_load_state("load", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    invalid = [f for f in discover(page) if f["invalid"]]
    errors = []
    for fr in page.frames:
        try:
            errors += fr.evaluate(ERROR_TEXT_JS)
        except Exception:
            continue

    content = ""
    try:
        content = page.content().lower()
    except Exception:
        pass

    captcha_still = any(w in content for w in (
        "recaptcha", "captcha", "verify you're human", "potvrdite",
        "sigurnosna provjera", "security check"))
    email_verify = any(w in content for w in (
        "confirm your email", "potvrdite svoju e-poštu",
        "check your inbox", "provjerite svoj inbox",
        "enter the confirmation code", "enter the code",
        "unesite kod", "we've sent", "poslali smo"))
    sms_verify = any(w in content for w in (
        "enter the code", "unesite kod", "confirm your phone"))
    success = any(w in content for w in (
        "welcome to facebook", "dobrodošli na facebook",
        "news feed", "newsfeed"))

    server_rejected = False
    if errors:
        joined = " ".join(errors).lower()
        if any(w in joined for w in (
                "unesite", "nije točno", "netočn", "važeć",
                "enter a", "please enter", "invalid", "required",
                "molimo", "pogreška", "greška", "error")):
            server_rejected = True

    url_changed = page.url != url_before

    # --- odluka -----------------------------------------------------------
    if success:
        accepted = True
        log(f"  ✓ SUCCESS. URL: {page.url}")
    elif email_verify:
        accepted = True
        log(f"  ✓ Registration accepted — verification code sent.")
    elif captcha_still:
        accepted = False
        log("  ✗ CAPTCHA still present after solving attempt.")
    elif sms_verify:
        accepted = False
        log("  ⚠ SMS verification required.")
    elif invalid:
        accepted = False
        log(f"  ✗ HTML5 invalid: {[f['label'] for f in invalid]}")
    elif server_rejected:
        accepted = False
        log(f"  ✗ Server rejected: {errors[:2]}")
    elif not url_changed:
        accepted = False
        log(f"  ✗ URL unchanged — likely shadow-rejected.")
    else:
        accepted = True
        log(f"  ✓ Accepted. URL: {page.url}")

    return {"submitted": accepted, "accepted": accepted, "method": method,
            "invalid": invalid, "page_errors": errors, "url_changed": url_changed}


def fix_invalid(invalid: list[dict], gen: DataGen, entries: list) -> int:
    """Replace values of invalid fields with generated ones that fit their constraints."""
    fixed = 0
    for f in invalid:
        # NE retryaj polja koja očito traže email/telefon — generirat će smeće
        label = norm(f.get("label", ""))
        if any(w in label for w in ("email", "epošte", "eposte", "mail",
                                     "mobitel", "telefon", "phone", "tel")):
            log(f"  [ ~ ] {f['label'][:26]!r:28} skip retry (email/phone field)")
            continue
        value = gen.generate(f)
        if value is None:
            continue
        result = fill_field(f, value)
        title = f["label"] or f["name"] or f["id"]
        entries.append({"field": title, "label_source": f["label_source"],
                        "key": "<retry>", "score": 0.0, "result": result,
                        "reason": f["message"]})
        log(f"  [ ~ ] {title[:26]!r:28} retry: {result}  ({f['message'] or 'invalid'})")
        fixed += 1
    return fixed


def fill_record(page: Page, record: dict, gen: DataGen) -> dict:
    record = {k: v for k, v in record.items() if not k.startswith("_")}
    used: set = set()
    entries: list = []
    prev_sig = None
    last = None
    for step in range(1, MAX_STEPS + 1):
        log(f" Step {step}")
        fields, last_here = fill_step(page, record, used, entries, gen)
        last = last_here or last
        sig = [(f["kind"], f["name"], f["id"], f["label"]) for f in fields]
        if sig == prev_sig:
            log("  Same step as before (validation?), stopping.")
            break
        prev_sig = sig
        wait_for_human_captcha(page)
        nxt = find_next(page)
        if nxt:
            nxt.click()
            page.wait_for_timeout(400)  # let the next step render
            continue
        break
    unused = [k for k in record if k not in used]
    if unused:
        log(f"  Unused keys: {unused}")
    result = {"filled": entries, "unused_keys": unused, "submitted": False,
              "method": None, "invalid_fields": [], "page_errors": [], "attempts": 0}
    if CFG.submit:
        wait_for_human_captcha(page)
        final_status = "rejected"
        for attempt in range(CFG.retries + 1):
            res = submit_form(page, last)
            result.update(submitted=res["submitted"], method=res["method"],
                          page_errors=res["page_errors"], attempts=attempt + 1)
            result["invalid_fields"] = [
                {"field": f["label"] or f["name"] or f["id"], "message": f["message"]}
                for f in res["invalid"]]

            if res.get("accepted"):
                final_status = "accepted"
                log(f"  ✓ Accepted on attempt {attempt + 1}.")
                break

            if attempt < CFG.retries:
                log(f"  ✗ Attempt {attempt + 1} rejected, retrying...")
                # Popravi HTML5 invalid polja
                if res["invalid"]:
                    if not fix_invalid(res["invalid"], gen, entries):
                        pass  # nema što popraviti, ali nastavi s retryem
                page.wait_for_timeout(800)
            else:
                log(f"  ✗ All {CFG.retries + 1} attempts failed.")
                break
        result["status"] = final_status
    return result


def login(page: Page, account: dict, default_url: str | None, gen: DataGen) -> None:
    """Log in with the same dynamic mechanism; the session stays in this context."""
    url = account.get("login_url") or default_url
    if not url:
        raise ValueError(f"No login URL for account {account['label']!r}")
    log(f"\nLogin as {account['label']!r}: {url}")
    page.goto(to_file_uri_if_path(url), timeout=30000,
              wait_until="domcontentloaded")
    handle_cookies(page)
    saved = CFG.submit
    CFG.submit = True
    try:
        fill_record(page, account["creds"], gen)
    finally:
        CFG.submit = saved
    page.wait_for_load_state()
    page.wait_for_timeout(500)


def append_jsonl(path: Path, result: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")


def run_session(task: dict) -> list[dict]:
    """One browser session per job: fresh browser → fresh everything.
    Stops early if Facebook starts blocking (CAPTCHA, anti-bot, rate limit)."""
    global CFG
    CFG = task["cfg"]
    out: Path = CFG.out
    gen = DataGen(CFG.faker_locale)
    account = task["account"]
    results = []

    ua = random_ua()
    viewport = random_viewport()
    locale, timezone = random_locale()
    proxy = task.get("proxy")

    # --- brojači za rano zaustavljanje ------------------------------------
    CONSECUTIVE_FAIL_LIMIT = 3   # prekini nakon 3 neuspjeha zaredom
    consecutive_fails = 0
    stopped_early = False

    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]

        # --- login u svom browseru (ako treba) ----------------------------
        login_failed = None
        if account:
            login_browser = p.chromium.launch(
                headless=CFG.headless, slow_mo=CFG.slow, args=launch_args)
            login_context = login_browser.new_context(
                user_agent=ua, viewport=viewport, locale=locale,
                timezone_id=timezone, proxy=proxy,
                extra_http_headers={"Accept-Language": f"{locale},en;q=0.9"},
            )
            login_context.add_init_script(STEALTH_JS)
            login_page = login_context.new_page()
            try:
                login(login_page, account, task["default_login_url"], gen)
            except Exception as e:
                login_failed = f"login failed: {e}"
                log(f"  {login_failed}")
            finally:
                try:
                    login_browser.close()
                except Exception:
                    pass

        # --- svaki job = SVJEŽI BROWSER -----------------------------------
        for job in task["jobs"]:
            res = {"job": job["id"], "account": account["label"] if account else None,
                   "url": job["url"], "row": job["row"], "status": "error", "error": None}
            log(f"\nJob {job['id']} | {job['url']} | row {job['row']}"
                + (f" | {account['label']}" if account else ""))

            browser = None
            context = None
            page = None
            blocked = False   # je li ovaj job bio blokiran?
            try:
                browser = p.chromium.launch(
                    headless=CFG.headless, slow_mo=CFG.slow, args=launch_args)

                context = browser.new_context(
                    user_agent=ua, viewport=viewport, locale=locale,
                    timezone_id=timezone, proxy=proxy,
                    extra_http_headers={"Accept-Language": f"{locale};en;q=0.9"},
                )

                if CFG.cookies_in and Path(CFG.cookies_in).exists():
                    try:
                        cookies = json.loads(
                            Path(CFG.cookies_in).read_text(encoding="utf-8"))
                        context.add_cookies(cookies)
                        log(f"  Loaded {len(cookies)} cookies")
                    except Exception as e:
                        log(f"  Failed to load cookies: {e}")

                context.add_init_script(STEALTH_JS)
                page = context.new_page()

                if login_failed:
                    raise RuntimeError(login_failed)

                page.goto(to_file_uri_if_path(job["url"]), timeout=30000,
                          wait_until="domcontentloaded")
                handle_cookies(page)

                # --- ako je browser odmah mrtav → anti-bot kill -------------
                if page.is_closed():
                    raise RuntimeError("browser killed immediately (anti-bot)")

                # --- confirmation page (Facebook i slični) -----------------
                if is_confirmation_page(page):
                    log("  ✓ Registration accepted — confirmation code page reached.")
                    res["status"] = "submitted"
                    res["final_url"] = page.url
                    res["confirmation_page"] = True
                    res["confirmation_url"] = page.url
                    consecutive_fails = 0   # uspjeh resetira brojač
                    try:
                        if not page.is_closed():
                            shot = out / f"job_{job['id']:03d}.png"
                            page.screenshot(path=str(shot), full_page=True)
                            res["screenshot"] = shot.name
                    except Exception:
                        pass
                    append_jsonl(out / "report.jsonl", res)
                    results.append(res)
                    continue

                if CFG.inspect:
                    res["fields"] = inspect_page(page, job["record"])
                    res["status"] = "inspected"
                    res["final_url"] = page.url
                else:
                    res.update(fill_record(page, job["record"], gen))
                    res["final_url"] = page.url
                    if CFG.submit:
                        res["status"] = ("submitted" if res["submitted"]
                                         else "not-submitted")
                        # Ako nije submitano, to je neuspjeh
                        if not res["submitted"]:
                            blocked = True
                    else:
                        res["status"] = "filled"

            except CaptchaBlocked as e:
                res.update(status="captcha", error=str(e))
                log(f"  Skipped: {e}")
                blocked = True
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                res.update(status="error", error=err)
                log(f"  Error: {err}")
                # Anti-bot kill je uvijek "blocked"
                if "TargetClosedError" in err or "anti-bot" in err.lower():
                    blocked = True
                else:
                    blocked = True   # svaka greška je neuspjeh

            # --- resetiraj ili povećaj brojač ----------------------------
            if blocked:
                consecutive_fails += 1
                log(f"  ✗ Failure streak: {consecutive_fails}/{CONSECUTIVE_FAIL_LIMIT}")
            else:
                consecutive_fails = 0

            # --- spremi cookies -------------------------------------------
            if CFG.cookies_out and context is not None:
                try:
                    cookies = context.cookies()
                    Path(CFG.cookies_out).write_text(
                        json.dumps(cookies, indent=2, ensure_ascii=False),
                        encoding="utf-8")
                    log(f"  Saved {len(cookies)} cookies to {CFG.cookies_out}")
                except Exception as e:
                    log(f"  Failed to save cookies: {e}")

            # --- screenshot -----------------------------------------------
            try:
                if page is not None and not page.is_closed():
                    shot = out / f"job_{job['id']:03d}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    res["screenshot"] = shot.name
            except Exception:
                pass

            append_jsonl(out / "report.jsonl", res)
            results.append(res)

            # --- ZATVORI browser za ovaj job ------------------------------
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass

            # --- provjeri je li vrijeme za prekid ------------------------
            if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                log(f"\n  ⛔ {CONSECUTIVE_FAIL_LIMIT} failures in a row — "
                    f"stopping the session early (Facebook is blocking).")
                stopped_early = True
                break

            # --- pause između jobova --------------------------------------
            if not CFG.headless and CFG.job_pause > 0:
                import time
                time.sleep(CFG.job_pause * random.uniform(0.7, 1.4))

        # --- final hold ---------------------------------------------------
        try:
            if not CFG.headless:
                if CFG.hold is None and CFG.interactive:
                    input("\nDone. Press Enter to close the browser...")
                else:
                    secs = CFG.hold if CFG.hold is not None else 15
                    if secs > 0:
                        log(f"\nDone. Waiting {secs:g}s before exit...")
                        import time
                        time.sleep(secs)
        except Exception:
            pass

    # --- zapamti je li sesija prekinuta -----------------------------------
    if stopped_early and results:
        results[-1]["stopped_early"] = True
        results[-1]["reason"] = f"{CONSECUTIVE_FAIL_LIMIT} failures in a row"

    return results

def human_type(loc, text: str) -> None:
    """Type text with human-like delays between keystrokes."""
    loc.click()
    loc.fill("")
    try:
        loc.press_sequentially(str(text), delay=random.uniform(30, 150))
    except AttributeError:
        # Starija verzija Playwrighta
        for ch in str(text):
            loc.type(ch, delay=random.uniform(30, 150))
            if random.random() < 0.05:
                loc.page.wait_for_timeout(random.uniform(200, 600))

def build_jobs(urls: list[str], records: list[dict], mode: str) -> list[dict]:
    """all = every URL x every row (a row's `_url` overrides); pair = url[i] with row[i]."""
    jobs = []

    def add(url, rec, row):
        jobs.append({"id": len(jobs) + 1, "url": rec.get("_url") or url,
                     "record": rec, "row": row})

    if mode == "pair":
        for i, rec in enumerate(records):
            add(urls[i % len(urls)], rec, i + 1)
    else:
        for url in urls:
            for i, rec in enumerate(records):
                if rec.get("_url") and url != urls[0]:
                    continue  # a row with its own _url is run only once
                add(url, rec, i + 1)
    return jobs


def main() -> None:
    global CFG
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", action="append", help="form URL or file:// (repeatable)")
    ap.add_argument("--urls-file", help=".txt (one URL per line) or .csv/.xlsx with a url column")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help=".csv/.xlsx/.json rows")
    ap.add_argument("--rows", type=int, help="ignore --data, generate N rows with Faker")
    ap.add_argument("--mode", choices=["all", "pair"], default="all",
                    help="all = every URL x every row; pair = url[i] with row[i]")
    ap.add_argument("--logins", help=".csv/.xlsx/.json accounts (username, password, login_url)")
    ap.add_argument("--login-url", help="default login page for accounts without login_url")
    ap.add_argument("--login-data", help="single account JSON (e.g. login.json)")
    ap.add_argument("--no-submit", action="store_true", help="only fill, do not submit")
    ap.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)  # legacy
    ap.add_argument("--fake-missing", action="store_true",
                    help="generate Faker values for fields without data")
    ap.add_argument("--faker-locale", default="en_US")
    ap.add_argument("--retries", type=int, default=1,
                    help="re-submit attempts after validation errors (default 1)")
    ap.add_argument("--workers", type=int, default=1, help="parallel browser sessions")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--slow", type=int, default=300, help="slow down actions (ms)")
    ap.add_argument("--next-text", action="append", default=[],
                    help="extra text of the 'next' button (repeatable)")
    ap.add_argument("--next-selector", help="CSS selector of the 'next' button")
    ap.add_argument("--submit-selector", help="CSS selector of the submit button")
    ap.add_argument("--captcha-timeout", type=int, default=180)
    ap.add_argument("--hold", type=float, default=None,
                    help="seconds to keep the browser open at the end "
                         "(default: wait for Enter, or 15s without a terminal)")
    ap.add_argument("--pause", type=float, default=2.0,
                    help="seconds to wait after each job in visible mode (default 2)")
    ap.add_argument("--debug", action="store_true",
                    help="print signals and top candidate keys for every field")
    ap.add_argument("--inspect", action="store_true",
                    help="only list the detected fields and best keys, fill nothing")
    ap.add_argument("--map", help='JSON file {"css selector": "data key" | "=literal"} '
                                  "that overrides automatic matching")
    ap.add_argument("--no-highlight", action="store_true",
                    help="do not outline fields while filling")
    ap.add_argument("--cookies", choices=["accept", "reject", "ignore"], default="accept",
                    help="what to do with cookie banners (default: accept)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="folder for report + screenshots")
    ap.add_argument("--proxy", help="Proxy URL: http://user:pass@host:port "
                                    "or socks5://host:port")
    ap.add_argument("--proxy-file", help=".txt file with one proxy per line "
                                         "(rotated per worker)")
    ap.add_argument("--cookies-in", help="load cookies from this JSON file before each job")
    ap.add_argument("--cookies-out", help="save cookies to this JSON file after each job")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.jsonl").unlink(missing_ok=True)

    CFG = Settings(
        headless=args.headless, slow=args.slow, submit=not args.no_submit,
        fake_missing=args.fake_missing or args.rows is not None,
        faker_locale=args.faker_locale, retries=max(0, args.retries),
        next_selector=args.next_selector, submit_selector=args.submit_selector,
        captcha_timeout=args.captcha_timeout, cookies=args.cookies, out=out, hold=args.hold, job_pause=args.pause,
        highlight=not args.no_highlight, debug=args.debug, inspect=args.inspect,
        field_map=json.loads(Path(args.map).read_text(encoding="utf-8")) if args.map else {},
        interactive=sys.stdin.isatty() and args.workers <= 1 and not args.headless,
    )
    CFG.next_words.extend(args.next_text)

    # --- inputs ------------------------------------------------------------
    urls = list(args.url or [])
    if args.urls_file:
        urls += load_urls(args.urls_file)
    urls = urls or [DEFAULT_URL]
    records = [{} for _ in range(args.rows)] if args.rows else load_table(args.data)
    if not records:
        ap.error("no data rows found")
    accounts = []
    if args.logins:
        accounts = load_logins(args.logins)
    elif args.login_data:
        creds = json.loads(Path(args.login_data).read_text(encoding="utf-8"))
        accounts = [{"label": creds.get("username", "account"), "login_url": None,
                     "creds": creds}]
    if accounts and not args.login_url and not all(a["login_url"] for a in accounts):
        ap.error("accounts without login_url need --login-url")

    jobs = build_jobs(urls, records, args.mode)
    log(f"{len(jobs)} job(s): {len(urls)} URL(s) x {len(records)} row(s), "
        f"{max(1, len(accounts))} account(s), {max(1, args.workers)} worker(s)")

    # --- proxy pool (rotated per worker / per account) ---------------------
    from urllib.parse import urlparse

    proxies: list[str] = []
    if args.proxy:
        proxies = [args.proxy]
    elif args.proxy_file:
        proxies = [line.strip() for line in
                   Path(args.proxy_file).read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.startswith("#")]

    def proxy_for(i: int):
        """Return a Playwright proxy dict for worker/account index i, or None."""
        if not proxies:
            return None
        p = proxies[i % len(proxies)]
        u = urlparse(p)
        d = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            d["username"] = u.username
        if u.password:
            d["password"] = u.password
        return d

    if proxies:
        log(f"  Using {len(proxies)} proxy(ies), rotated per session.")

    # --- tasks: one per account, or the jobs split across workers ----------
    if accounts:
        tasks = [{"account": a, "jobs": jobs, "default_login_url": args.login_url,
                  "cfg": CFG, "proxy": proxy_for(i)}
                 for i, a in enumerate(accounts)]
    else:
        n = max(1, args.workers)
        chunks = [jobs[i::n] for i in range(n) if jobs[i::n]]
        tasks = [{"account": None, "jobs": c, "default_login_url": None, "cfg": CFG,
                  "proxy": proxy_for(i)}
                 for i, c in enumerate(chunks)]

    results: list[dict] = []
    if args.workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            for res in pool.map(run_session, tasks):
                results.extend(res)
    else:
        for t in tasks:
            results.extend(run_session(t))

    results.sort(key=lambda r: (r.get("account") or "", r["job"]))
    (out / "report.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    log(f"\nDone: {summary}")
    log(f"Report and screenshots: {out}")



if __name__ == "__main__":
    main()

