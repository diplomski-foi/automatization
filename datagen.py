"""Constraint-aware fake data for form fields (Faker + a tiny regex generator)."""
import random
import re
import string

from faker import Faker

try:  # private but stable since Python 3.11
    import re._parser as _sre
except Exception:  # pragma: no cover
    _sre = None

# (regex over normalized hint strings, generator name); order matters.
SEMANTIC = [
    (r"email|mail", "email"),
    (r"phone|^tel$|^telno|^tel\d|mobile|^cell$|^cellphone", "phone"),
    (r"firstname|givenname|forename|fname", "first_name"),
    (r"lastname|surname|familyname|lname", "last_name"),
    (r"fullname|^name$|yourname", "name"),
    (r"username|userid|nickname|login|handle", "user_name"),
    (r"password|passwd|pwd", "password"),
    (r"zip|postal|postcode", "postcode"),
    (r"city|town", "city"),
    (r"country|nation", "country"),
    (r"state|province|region", "state"),
    (r"address|street", "street_address"),
    (r"company|organi[sz]ation|employer", "company"),
    (r"jobtitle|position|occupation|profession", "job"),
    (r"url|website|homepage|link", "url"),
    (r"birth|dob|bday", "birthdate"),
    (r"^age$|^yourage$", "age"),
    (r"message|comment|note|description|about|bio|feedback|text|yourself", "paragraph"),
]


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


class DataGen:
    def __init__(self, locale: str = "en_US", seed: int | None = None):
        self.fake = Faker(locale)
        if seed is not None:
            Faker.seed(seed)
        self.rng = random.Random(seed)

    # ---- public -----------------------------------------------------------
    def generate(self, f: dict):
        """Return a value for field dict `f`, or None if it cannot be generated."""
        kind = f["kind"]
        if kind in ("select", "radio"):
            opts = [o for o in f.get("options", []) if o.get("value") not in ("", None)]
            if not opts:
                return None
            o = self.rng.choice(opts)
            return o["label"] or o["value"]
        if kind in ("combobox", "file"):
            return None
        if kind == "checkbox":
            return True
        value = self._by_kind(f, kind)
        return self._constrain(f, value)

    # ---- internals --------------------------------------------------------
    def _semantic(self, f: dict) -> str | None:
        pieces = [_norm(f.get(k, "")) for k in ("label", "name", "id", "placeholder")]
        ac = f.get("autocomplete", "").split()
        if ac:
            pieces.append(_norm(ac[-1]))
        for rx, gen in SEMANTIC:
            if any(p and re.search(rx, p) for p in pieces):
                return gen
        return None

    def _named(self, gen: str):
        fk = self.fake
        return {
            "email": fk.email, "phone": fk.phone_number, "first_name": fk.first_name,
            "last_name": fk.last_name, "name": fk.name, "user_name": fk.user_name,
            "password": lambda: fk.password(length=12), "postcode": fk.postcode,
            "city": fk.city, "country": fk.country, "state": fk.state,
            "street_address": fk.street_address, "company": fk.company, "job": fk.job,
            "url": fk.url, "birthdate": lambda: fk.date_of_birth(minimum_age=18).isoformat(),
            "age": lambda: str(self.rng.randint(18, 70)),
            "paragraph": lambda: fk.sentence(nb_words=10),
        }[gen]()

    def _by_kind(self, f: dict, kind: str):
        fk = self.fake
        if kind == "email":
            return fk.email()
        if kind == "tel":
            return fk.phone_number()
        if kind == "url":
            return fk.url()
        if kind == "password":
            return fk.password(length=12)
        if kind == "date":
            return fk.date_of_birth(minimum_age=18).isoformat()
        if kind == "time":
            return fk.time(pattern="%H:%M")
        if kind == "datetime-local":
            return fk.date_time_this_year().strftime("%Y-%m-%dT%H:%M")
        if kind == "month":
            return fk.date_this_year().strftime("%Y-%m")
        if kind == "week":
            return fk.date_this_year().strftime("%Y-W%V")
        if kind == "color":
            return fk.hex_color()
        if kind in ("number", "range"):
            lo = _int(f.get("min")) if _int(f.get("min")) is not None else 1
            hi = _int(f.get("max")) if _int(f.get("max")) is not None else lo + 100
            return str(self.rng.randint(lo, max(lo, hi)))
        gen = self._semantic(f)
        if gen:
            return self._named(gen)
        return fk.word() if kind != "textarea" else fk.sentence(nb_words=12)

    def _constrain(self, f: dict, value):
        if value is None:
            return None
        value = str(value)
        pattern = f.get("pattern")
        if pattern:
            ok = self._fullmatch(pattern, value)
            tries = 0
            while not ok and tries < 15:
                tries += 1
                value = self._by_kind(f, f["kind"])
                ok = self._fullmatch(pattern, str(value))
            if not ok:
                value = self._from_pattern(pattern) or value
        value = str(value)
        mx, mn = _int(f.get("maxlength")), _int(f.get("minlength"))
        if mx is not None and mx >= 0:
            value = value[:mx]
        if mn and len(value) < mn:
            value = value + "x" * (mn - len(value))
        return value

    @staticmethod
    def _fullmatch(pattern: str, value: str) -> bool:
        try:  # HTML pattern attribute is an implicit ^(?:...)$
            return re.fullmatch(pattern, value) is not None
        except re.error:
            return True

    # ---- tiny regex-based generator for `pattern` attributes --------------
    def _from_pattern(self, pattern: str) -> str | None:
        if _sre is None:
            return None
        try:
            parsed = _sre.parse(pattern)
            return "".join(self._emit(parsed))
        except Exception:
            return None

    def _chars_of_class(self, items) -> str:
        negate = any(str(op) == "NEGATE" for op, _ in items)
        chars = ""
        for op, av in items:
            name = str(op)
            if name == "LITERAL":
                chars += chr(av)
            elif name == "RANGE":
                chars += "".join(chr(c) for c in range(av[0], min(av[1], av[0] + 200) + 1))
            elif name == "CATEGORY":
                cat = str(av)
                chars += (string.digits if "DIGIT" in cat and "NOT" not in cat
                          else string.ascii_letters + string.digits + "_" if "WORD" in cat
                          else " " if "SPACE" in cat else string.ascii_letters)
        if negate:
            pool = string.ascii_letters + string.digits
            return "".join(c for c in pool if c not in chars) or "x"
        return chars or "x"

    def _emit(self, parsed):
        out = []
        for op, av in parsed:
            name = str(op)
            if name == "LITERAL":
                out.append(chr(av))
            elif name == "NOT_LITERAL":
                out.append(self.rng.choice([c for c in string.ascii_letters if c != chr(av)]))
            elif name == "ANY":
                out.append(self.rng.choice(string.ascii_letters))
            elif name == "IN":
                out.append(self.rng.choice(self._chars_of_class(av)))
            elif name == "CATEGORY":
                cat = str(av)
                out.append(self.rng.choice(string.digits if "DIGIT" in cat
                                           else string.ascii_letters))
            elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                lo, hi, sub = av
                hi = min(hi, lo + 5) if hi > lo + 5 else hi
                for _ in range(self.rng.randint(lo, hi)):
                    out.extend(self._emit(sub))
            elif name == "SUBPATTERN":
                out.extend(self._emit(av[-1]))
            elif name == "BRANCH":
                out.extend(self._emit(self.rng.choice(av[1])))
        return out
