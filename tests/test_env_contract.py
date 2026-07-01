"""Contract guard for the committed `.env.example` template (P4 integration/contract).

`.env.example` is what every operator copies to `.env`, and `.env` is loaded by BOTH
`docker run --env-file` and docker-compose `env_file:`. The `--env-file` path takes the text after
`=` LITERALLY — it does NOT strip a trailing `# comment` the way python-dotenv does — so a line like
`POINT_CAP=2000   # max points` injects the value `2000   # max points`, which then fails int
parsing and crashes the API at boot. These pure-logic tests (no DB) pin the template so a verbatim
copy is safe through every load path:

  1. format: comments live on their own column-0 line; value lines are bare (no inline `#`);
  2. drift: `.env.example` must NOT hand-pin EXPECTED_SCHEMA_VERSION — doing so silently overrides
     the image's own value and re-arms the corrections-500 'new code vs old schema' drift (the very
     thing tests/test_schema_contract.py + the boot guard exist to prevent);
  3. numerics: every value that maps to an `int` Settings field actually parses as an int (a second
     line of defense against a smuggled-in inline comment or a fat-fingered number).
"""
import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
_KEY = re.compile(r"^[A-Z][A-Z0-9_]*=")


def _lines() -> list[str]:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _pairs() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _lines():
        if _KEY.match(line):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_env_example_exists_and_is_nonempty():
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"
    assert _pairs(), "no KEY=VALUE lines parsed from .env.example"


def test_every_line_is_blank_a_column0_comment_or_a_bare_key_value():
    for i, line in enumerate(_lines(), 1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            # A comment with leading whitespace is itself a footgun: classic `docker --env-file`
            # only treats a line as a comment when `#` is the FIRST character.
            assert line.startswith("#"), (
                f".env.example:{i}: comment must start at column 0 (no leading whitespace): {line!r}"
            )
            continue
        assert _KEY.match(line), f".env.example:{i}: not a KEY=VALUE line: {line!r}"
        value = line.partition("=")[2]
        assert "#" not in value, (
            f".env.example:{i}: value carries an inline comment / '#': {line!r}. "
            "`docker run --env-file` takes the text after '=' literally, so the '#...' becomes part "
            "of the value and breaks parsing at boot. Move the comment to its own column-0 line."
        )


def test_does_not_hand_pin_expected_schema_version():
    assert "EXPECTED_SCHEMA_VERSION" not in _pairs(), (
        ".env.example must NOT set EXPECTED_SCHEMA_VERSION. Pinning it in .env silently overrides "
        "the value baked into the image (backend/app/config.py) and WILL drift behind the next "
        "migration — re-arming the exact 'new code vs old schema' drift the boot guard exists to "
        "prevent (corrections-500). Leave the schema head to the image."
    )


def test_int_valued_settings_parse_as_int():
    pairs = _pairs()
    int_fields = {
        name.upper() for name, fi in Settings.model_fields.items() if fi.annotation is int
    }
    checked = 0
    for key in int_fields & pairs.keys():
        raw = pairs[key]
        try:
            int(raw)
        except ValueError:
            raise AssertionError(
                f".env.example: {key}={raw!r} does not parse as an int — an inline comment or a "
                "malformed number would crash the API at boot when loaded via --env-file."
            ) from None
        checked += 1
    assert checked >= 5, f"expected to validate several int settings, only checked {checked}"
