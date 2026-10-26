"""
Tests for the translations (docs/TRANSLATIONS.md): the two catalogs, their templates,
the marks in the sources and the packaging of the .mo files.

Each checker is a function that takes the text or the tree it checks, so a negative test
gives it a bad copy; the repository as it is has no problems.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import ast
import glob
import importlib.util
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GNOME_DOMAIN = "NetworkManager-gpclient"
PLASMA_DOMAIN = "plasmanetworkmanagement_gpclientui"
GNOME_EDITORS = ["plugins/gnome/nm-gpclient-editor.c", "plugins/gnome/nm-gpclient-editor-gtk3.c"]

needs_gettext = pytest.mark.skipif(
    not (shutil.which("xgettext") and shutil.which("msgfmt")),
    reason="xgettext and msgfmt (the gettext package) are not installed",
)


def read(name, root=ROOT):
    with open(os.path.join(root, name), encoding="utf-8") as handle:
        return handle.read()


def load_update_pot():
    spec = importlib.util.spec_from_file_location("update_pot", os.path.join(ROOT, "scripts", "update_pot.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def languages(root=ROOT):
    lines = (line.split("#")[0].strip() for line in read("po/LINGUAS", root).splitlines())
    return [line for line in lines if line]


def catalog_paths(lang, root=ROOT):
    return [
        os.path.join(root, "po", lang + ".po"),
        os.path.join(root, "plugins", "plasma", "po", lang, PLASMA_DOMAIN + ".po"),
    ]


# ---- the .po / .pot format -------------------------------------------------------------


def parse_po(text):
    """[(msgid, msgstr, flags)] of a .po or .pot text (header included, as msgid "")"""
    entries = []
    for block in re.split(r"\n\s*\n", text):
        if "msgid" not in block:
            continue
        flags = " ".join(re.findall(r"^#, (.*)$", block, re.M))
        fields = {}
        current = None
        for line in block.splitlines():
            match = re.match(r"(msgctxt|msgid|msgstr)\s+(\".*\")$", line)
            if match:
                current = match.group(1)
                fields[current] = ast.literal_eval(match.group(2))
            elif line.startswith('"') and current:
                fields[current] += ast.literal_eval(line)
        entries.append((fields.get("msgid", ""), fields.get("msgstr", ""), flags))
    return entries


def msgid_set(text):
    return {msgid for msgid, _, _ in parse_po(text) if msgid}


def msgid_diff(committed, fresh):
    """What differs between the msgids of the committed template and a fresh one"""
    return {"missing": sorted(fresh - committed), "obsolete": sorted(committed - fresh)}


# An accelerator may stand before any letter (_Отмена, _إلغاء) and inside a word (İ_ptal)
PLACEHOLDER = re.compile(r"%(?:\d+\$)?[sdiufc]|</?[A-Za-z][^<>]*>|[_&](?=[^\W\d_])")
NUMBERED = re.compile(r"%\d+")


def translation_problems(entries):
    """What is wrong with the entries of a translation: untranslated or fuzzy entries and
    placeholders (%s, %d, %1, markup, accelerators) that differ from the original"""
    problems = []
    for msgid, msgstr, flags in entries:
        if not msgid:
            continue
        if "fuzzy" in flags:
            problems.append(f"fuzzy: {msgid!r}")
        if not msgstr:
            problems.append(f"untranslated: {msgid!r}")
            continue
        # printf placeholders, tags and accelerators: same count and order; %1..%n may move
        # (KDE and Qt allow that), so only their set is compared
        plain = [p for p in PLACEHOLDER.findall(msgid) if not NUMBERED.fullmatch(p)]
        plain_tr = [p for p in PLACEHOLDER.findall(msgstr) if not NUMBERED.fullmatch(p)]
        if plain != plain_tr:
            problems.append(f"placeholders {plain} != {plain_tr}: {msgid!r}")
        if sorted(NUMBERED.findall(msgid)) != sorted(NUMBERED.findall(msgstr)):
            problems.append(f"numbered placeholders differ: {msgid!r}")
    return problems


def header_problems(text, lang):
    """The header of a translation: language, charset, plural forms, translator comment"""
    problems = []
    if not re.search(rf'^"Language: {lang}\\n"$', text, re.M):
        problems.append("no Language header")
    if "charset=UTF-8" not in text:
        problems.append("charset is not UTF-8")
    if not re.search(r'^"Plural-Forms: nplurals=\d+; plural=', text, re.M):
        problems.append("no Plural-Forms")
    return problems


def msgfmt_problems(path):
    """The output of `msgfmt --check-format -c` for a .po file, empty when it is fine"""
    result = subprocess.run(
        ["msgfmt", "--check-format", "-c", "-o", os.devnull, path], capture_output=True, text=True
    )
    return result.stderr if result.returncode else ""


def missing_catalogs(root):
    """The .po files that LINGUAS names and the tree does not have"""
    return [p for lang in languages(root) for p in catalog_paths(lang, root) if not os.path.isfile(p)]


# ---- the marks in the sources ----------------------------------------------------------

MARK = re.compile(r'(?<!\w)N?_\(\s*"')
SOURCE_GLOBS = ["plugins/gnome/*.c", "plugins/gnome/*.h", "auth-dialog/*.py"]


def potfiles(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def potfiles_problems(root, listed):
    """Files with _() or N_() that the list lacks, and listed files that do not exist or have none"""
    marked = set()
    for pattern in SOURCE_GLOBS:
        for path in glob.glob(os.path.join(root, pattern)):
            if MARK.search(read(os.path.relpath(path, root), root)):
                marked.add(os.path.relpath(path, root))
    problems = [f"not listed: {p}" for p in sorted(marked - set(listed))]
    for path in listed:
        if not os.path.isfile(os.path.join(root, path)):
            problems.append(f"missing file: {path}")
        elif path not in marked:
            problems.append(f"no translatable string: {path}")
    return problems


def split_args(text, start):
    """The top-level arguments of the call whose "(" is at text[start]"""
    args, depth, i, current = [], 0, start, ""
    while i < len(text):
        c = text[i]
        if c == '"':
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            current += text[i : j + 1]
            i = j
        elif c in "([":
            depth += 1
            current += c if depth > 1 else ""
        elif c in ")]":
            depth -= 1
            if depth == 0:
                args.append(current.strip())
                return args
            current += c
        elif c == "," and depth == 1:
            args.append(current.strip())
            current = ""
        else:
            current += c
        i += 1
    return args


# Calls that show their text argument to the user
SHOWN = (
    "gtk_label_new|gtk_label_set_text|gtk_label_set_markup|gtk_button_new_with_label|"
    "gtk_check_button_new_with_label|gtk_widget_set_tooltip_text|gtk_entry_set_placeholder_text|"
    "gtk_combo_box_text_append_text|gtk_window_set_title|g_set_error_literal|g_set_error|"
    "g_value_set_string"
)
SHOWN_CALL = re.compile(rf"\b({SHOWN})\s*\(")
# an empty text shows nothing, so it needs no translation (gtk_editable_set_text (entry, ""));
# the plugin's name (a proper noun) and its D-Bus service name are not translated
ALLOWED_LITERALS = {'""', '"GlobalProtect"', '"org.freedesktop.NetworkManager.gpclient"'}


def unwrapped_strings(text):
    """The text literals that a call shows to the user without _() (N_() only
    marks a text, it does not translate it)"""
    found = []
    for match in SHOWN_CALL.finditer(text):
        for arg in split_args(text, match.end() - 1):
            if (arg.startswith('"') and arg not in ALLOWED_LITERALS) or arg.startswith("N_("):
                found.append(f"{match.group(1)}: {arg[:50]}")
    return found


C_MSGID = re.compile(r'(?<!\w)N?_\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+)\)')


def c_msgids(text):
    return {
        "".join(ast.literal_eval(part) for part in re.findall(r'"(?:[^"\\]|\\.)*"', match.group(1)))
        for match in C_MSGID.finditer(text)
    }


def editor_msgid_diff(gtk4_text, gtk3_text):
    a, b = c_msgids(gtk4_text), c_msgids(gtk3_text)
    return {"only in GTK4": sorted(a - b), "only in GTK3": sorted(b - a)}


QT_SHOWN = re.compile(
    r'\b(addItem|setToolTip|setPlaceholderText|setWindowTitle|setSuffix|setWhatsThis)\s*\(\s*'
    r'(?:(?:QStringLiteral|QLatin1String)\s*\(\s*)?"'
)


def unwrapped_qt_strings(text):
    """Text literals that a Qt call shows without i18n()"""
    return [m.group(0) for m in QT_SHOWN.finditer(text)]


def translation_domain_problems(cmake_text):
    if not re.search(
        rf'^\s*add_definitions\(\s*-DTRANSLATION_DOMAIN=\\"{PLASMA_DOMAIN}\\"\s*\)', cmake_text, re.M
    ):
        return [f'CMakeLists.txt does not define TRANSLATION_DOMAIN "{PLASMA_DOMAIN}"']
    return []


def gettext_define_problems(makefile_text):
    """Every compile line of the GNOME objects gets the same GETTEXT_PACKAGE and LOCALEDIR"""
    problems = []
    if f'-DGETTEXT_PACKAGE=\\"{GNOME_DOMAIN}\\"' not in makefile_text or "-DLOCALEDIR=" not in makefile_text:
        return ["no I18N_CFLAGS with GETTEXT_PACKAGE and LOCALEDIR"]
    for name in ("PLUGIN_CFLAGS", "EDITOR_GTK3_CFLAGS", "EDITOR_GTK4_CFLAGS"):
        line = re.search(rf"^{name} =.*$", makefile_text, re.M)
        if not line or "$(I18N_CFLAGS)" not in line.group(0):
            problems.append(f"{name} has no $(I18N_CFLAGS)")
    return problems


def build_depends(text):
    match = re.search(r"^Build-Depends:(.*?)(?=^\S)", text, re.M | re.S)
    return match.group(1) if match else ""


# ---- tests: sources --------------------------------------------------------------------


def test_potfiles_lists_every_source_with_marks_and_only_those():
    assert potfiles_problems(ROOT, potfiles(read("po/POTFILES.in"))) == []


@pytest.fixture
def tree(tmp_path):
    """A small tree with one marked C file and one without marks"""
    (tmp_path / "plugins" / "gnome").mkdir(parents=True)
    (tmp_path / "auth-dialog").mkdir()
    (tmp_path / "plugins/gnome/a.c").write_text('x = _("Hello");\n')
    (tmp_path / "plugins/gnome/b.c").write_text("x = 1; /* uses _() in a comment */\n")
    return str(tmp_path)


def test_potfiles_accepts_a_complete_list(tree):
    assert potfiles_problems(tree, ["plugins/gnome/a.c"]) == []


@pytest.mark.parametrize(
    "listed, word",
    [
        ([], "not listed: plugins/gnome/a.c"),
        (["plugins/gnome/a.c", "plugins/gnome/b.c"], "no translatable string: plugins/gnome/b.c"),
        (["plugins/gnome/a.c", "plugins/gnome/gone.c"], "missing file: plugins/gnome/gone.c"),
    ],
)
def test_potfiles_rejects_a_wrong_list(tree, listed, word):
    assert word in potfiles_problems(tree, listed)


@pytest.mark.parametrize(
    "path", [p for p in potfiles(read("po/POTFILES.in")) if p.endswith((".c", ".h"))]
)
def test_the_c_sources_wrap_every_shown_string(path):
    assert unwrapped_strings(read(path)) == []


def test_an_unwrapped_string_in_a_header_is_found():
    # The shared editor header is checked like the .c files
    header = read("plugins/gnome/nm-gpclient-editor-common.h")
    assert "plugins/gnome/nm-gpclient-editor-common.h" in read("po/POTFILES.in")
    assert unwrapped_strings(header) == []

    broken = header + '\nstatic void f (GtkWidget *w) { gtk_widget_set_tooltip_text (w, "Raw text"); }\n'

    assert unwrapped_strings(broken)


@pytest.mark.parametrize(
    "snippet",
    [
        'label = gtk_label_new ("Gateway:");',
        'gtk_widget_set_tooltip_text (w,\n    "Tip one "\n    "tip two");',
        'gtk_combo_box_text_append_text (GTK_COMBO_BOX_TEXT (c), "Never");',
        'g_set_error_literal (error, NM_VPN_PLUGIN_ERROR, 1, "bad value");',
        'gtk_entry_set_placeholder_text (GTK_ENTRY (e), "Optional");',
        'gtk_combo_box_text_append_text (c, N_("Never"));',
        'g_value_set_string (value, "GlobalProtect VPN Client");',
    ],
)
def test_an_unwrapped_shown_string_is_found(snippet):
    assert unwrapped_strings(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        'label = gtk_label_new (_("Gateway:"));',
        'gtk_widget_set_tooltip_text (w,\n    _("Tip one "\n    "tip two"));',
        'gtk_combo_box_text_append_text (GTK_COMBO_BOX_TEXT (c), browser_values[i]);',
        'gtk_editable_set_text (GTK_EDITABLE (entry), "");',
        'g_set_error_literal (error, NM_VPN_PLUGIN_ERROR, 1, _(problem));',
        'g_value_set_string (value, "GlobalProtect");',
    ],
)
def test_a_wrapped_or_not_shown_string_is_fine(snippet):
    assert unwrapped_strings(snippet) == []


def test_the_gtk3_and_gtk4_editors_have_the_same_msgids():
    assert editor_msgid_diff(read(GNOME_EDITORS[0]), read(GNOME_EDITORS[1])) == {
        "only in GTK4": [],
        "only in GTK3": [],
    }


def test_a_msgid_in_one_editor_only_is_found():
    diff = editor_msgid_diff('a = _("One"); b = _("Two");', 'a = _("One");')
    assert diff == {"only in GTK4": ["Two"], "only in GTK3": []}


def test_the_msgids_join_the_pieces_of_a_string():
    assert c_msgids('x = _("Tip one "\n  "tip \\"two\\"");') == {'Tip one tip "two"'}


def test_the_plasma_sources_wrap_every_shown_string():
    for path in glob.glob(os.path.join(ROOT, "plugins/plasma/*.cpp")):
        assert unwrapped_qt_strings(read(os.path.relpath(path, ROOT))) == [], path


@pytest.mark.parametrize(
    "snippet",
    ['box->addItem(QStringLiteral("Never"));', 'w->setToolTip("Tip");', 'w->setPlaceholderText(QLatin1String("x"));'],
)
def test_an_unwrapped_qt_string_is_found(snippet):
    assert unwrapped_qt_strings(snippet)


@pytest.mark.parametrize(
    "snippet", ['box->addItem(i18n("Never"));', "box->addItem(entry);", 'data.insert(QStringLiteral("k"), v);']
)
def test_a_wrapped_qt_string_is_fine(snippet):
    assert unwrapped_qt_strings(snippet) == []


def test_the_cmake_file_defines_the_translation_domain():
    assert translation_domain_problems(read("plugins/plasma/CMakeLists.txt")) == []


@pytest.mark.parametrize(
    "text",
    [
        "project(x)\nadd_library(a MODULE a.cpp)\n",
        'add_definitions(-DTRANSLATION_DOMAIN=\\"other_domain\\")\n',
        '# add_definitions(-DTRANSLATION_DOMAIN=\\"plasmanetworkmanagement_gpclientui\\")\n',
    ],
)
def test_a_cmake_text_without_the_domain_fails(text):
    assert translation_domain_problems(text)


def test_the_gnome_objects_get_the_same_gettext_package():
    assert gettext_define_problems(read("plugins/gnome/Makefile")) == []
    for path in glob.glob(os.path.join(ROOT, "plugins/gnome/*.[ch]")):
        assert "#define GETTEXT_PACKAGE" not in read(os.path.relpath(path, ROOT)), path


@pytest.mark.parametrize(
    "old, new",
    [
        ("-DGETTEXT_PACKAGE=", "-DOTHER="),
        ("EDITOR_GTK3_CFLAGS = `pkg-config --cflags glib-2.0 libnm gtk+-3.0 libnma` $(I18N_CFLAGS)",
         "EDITOR_GTK3_CFLAGS = `pkg-config --cflags glib-2.0 libnm gtk+-3.0 libnma`"),
        ("$(I18N_CFLAGS) -Wall -fPIC\nPLUGIN_LIBS", "-Wall -fPIC\nPLUGIN_LIBS"),
    ],
)
def test_a_makefile_without_the_define_fails(old, new):
    text = read("plugins/gnome/Makefile")
    assert old in text
    assert gettext_define_problems(text.replace(old, new, 1))


@pytest.mark.parametrize(
    # NM loads the simple plugin (the .name file), which loads the editor
    "path",
    ["plugins/gnome/nm-gpclient-editor-plugin.c", "plugins/gnome/nm-gpclient-plugin-simple.c"],
)
def test_the_plugins_bind_the_text_domain(path):
    text = read(path)
    assert "#include <glib/gi18n-lib.h>" in text
    assert "bindtextdomain (GETTEXT_PACKAGE, LOCALEDIR)" in text
    assert 'bind_textdomain_codeset (GETTEXT_PACKAGE, "UTF-8")' in text


def test_the_validation_messages_are_marked_and_translated_where_they_are_shown():
    assert "return N_(" in read("plugins/gnome/nm-gpclient-validate.h")
    for path in GNOME_EDITORS:
        assert "_(problem)" in read(path)


# ---- tests: catalogs -------------------------------------------------------------------


def test_every_language_has_both_catalogs():
    assert languages() and missing_catalogs(ROOT) == []


def test_a_language_without_a_catalog_is_found(tmp_path):
    (tmp_path / "po").mkdir()
    (tmp_path / "po" / "LINGUAS").write_text("# comment\npl\nde\n")
    (tmp_path / "po" / "pl.po").write_text("")
    missing = missing_catalogs(str(tmp_path))
    assert [os.path.relpath(p, tmp_path) for p in missing] == [
        f"plugins/plasma/po/pl/{PLASMA_DOMAIN}.po",
        "po/de.po",
        f"plugins/plasma/po/de/{PLASMA_DOMAIN}.po",
    ]


@needs_gettext
@pytest.mark.parametrize(
    "pot, make",
    [
        (f"po/{GNOME_DOMAIN}.pot", "gnome_pot"),
        (f"plugins/plasma/po/{PLASMA_DOMAIN}.pot", "plasma_pot"),
    ],
)
def test_the_committed_template_is_up_to_date(tmp_path, pot, make):
    out = str(tmp_path / "fresh.pot")
    getattr(load_update_pot(), make)(ROOT, out)
    assert msgid_diff(msgid_set(read(pot)), msgid_set(open(out, encoding="utf-8").read())) == {
        "missing": [],
        "obsolete": [],
    }


def test_an_out_of_date_template_is_found():
    committed = msgid_set('msgid ""\nmsgstr ""\n\nmsgid "Old"\nmsgstr ""\n\nmsgid "Same"\nmsgstr ""\n')
    fresh = msgid_set('msgid ""\nmsgstr ""\n\nmsgid "New"\nmsgstr ""\n\nmsgid "Same"\nmsgstr ""\n')
    assert msgid_diff(committed, fresh) == {"missing": ["New"], "obsolete": ["Old"]}


def test_the_ui_strings_are_read_like_extractrc(tmp_path):
    ui = tmp_path / "a.ui"
    ui.write_text(
        '<ui><widget><property name="text"><string>One\nTwo &lt;x&gt;</string></property>'
        '<property name="a"><string notr="true">skip</string></property>'
        '<property name="b"><string comment="ctx">With context</string></property></widget></ui>'
    )
    assert load_update_pot().ui_strings(str(ui)) == [("One\nTwo <x>", None), ("With context", "ctx")]


def all_catalogs():
    return [path for lang in languages() for path in catalog_paths(lang)]


@needs_gettext
@pytest.mark.parametrize("path", all_catalogs(), ids=lambda p: os.path.relpath(p, ROOT))
def test_every_catalog_compiles(path):
    assert msgfmt_problems(path) == ""


@needs_gettext
@pytest.mark.parametrize("path", [f"po/{GNOME_DOMAIN}.pot", f"plugins/plasma/po/{PLASMA_DOMAIN}.pot"])
def test_the_templates_are_valid(path):
    assert msgfmt_problems(os.path.join(ROOT, path)) == ""


HEAD = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'


@needs_gettext
@pytest.mark.parametrize(
    "body",
    [
        '#, c-format\nmsgid "Value %s"\nmsgstr "Wartosc %d"\n',  # wrong format directive
        '#, c-format\nmsgid "Value %s"\nmsgstr "Wartosc"\n',  # directive lost
        'msgid "One"\nmsgstr "Jeden\n',  # unterminated string
        'msgid "One"\nmsgid "Two"\nmsgstr "Dwa"\n',  # two msgids in a row
    ],
)
def test_a_broken_catalog_fails_the_check(tmp_path, body):
    broken = tmp_path / "x.po"
    broken.write_text(HEAD + body)
    assert msgfmt_problems(str(broken))


@needs_gettext
def test_a_good_catalog_passes_the_check(tmp_path):
    good = tmp_path / "x.po"
    good.write_text(HEAD + '#, c-format\nmsgid "Value %s"\nmsgstr "Wartosc %s"\n')
    assert msgfmt_problems(str(good)) == ""


@pytest.mark.parametrize("path", all_catalogs(), ids=lambda p: os.path.relpath(p, ROOT))
def test_the_translation_is_complete_and_keeps_the_placeholders(path):
    entries = parse_po(open(path, encoding="utf-8").read())
    assert len(entries) > 1
    assert translation_problems(entries) == []


@pytest.mark.parametrize(
    "entry, word",
    [
        (("Hello", "", ""), "untranslated"),
        (("Hello", "Czesc", "fuzzy"), "fuzzy"),
        (("Hello %s", "Czesc", "c-format"), "placeholders"),
        (("Hello %s", "Czesc %d", "c-format"), "placeholders"),
        (("From %s to %d", "Do %d z %s", "c-format"), "placeholders"),
        (("Hello", "Czesc %s", ""), "placeholders"),
        (("Use <b>this</b>", "Uzyj tego", ""), "placeholders"),
        (("Use <b>this</b>", "Uzyj <i>tego</i>", ""), "placeholders"),
        (("_Cancel", "Anuluj", ""), "placeholders"),
        (("_Cancel", "Отмена", ""), "placeholders"),
        (("&Cancel", "Anuluj", ""), "placeholders"),
        (("From %1 to %2", "Od %1", ""), "numbered"),
        (("From %1", "Od %2", ""), "numbered"),
    ],
)
def test_a_bad_translation_is_found(entry, word):
    assert any(word in problem for problem in translation_problems([entry]))


@pytest.mark.parametrize(
    "entry",
    [
        ("Hello %s", "Czesc %s", "c-format"),
        ("From %s to %d", "Z %s do %d", "c-format"),
        ("From %1 to %2", "Do %2 z %1", ""),
        ("Use <b>this</b>", "Uzyj <b>tego</b>", ""),
        ("_Cancel", "_Anuluj", ""),
        ("_Cancel", "_Отмена", ""),
        ("_Cancel", "İ_ptal", ""),
        ("_OK", "OK(_O)", ""),
        ("--hip=<path>", "--hip=<path>", ""),
    ],
)
def test_a_good_translation_is_accepted(entry):
    assert translation_problems([entry]) == []


@pytest.mark.parametrize(
    "lang, path",
    [(lang, path) for lang in languages() for path in catalog_paths(lang)],
    ids=lambda v: os.path.relpath(v, ROOT) if os.path.isabs(v) else v,
)
def test_every_catalog_has_its_header(lang, path):
    # The review note is not checked: a reviewer removes it (docs/TRANSLATIONS.md)
    text = open(path, encoding="utf-8").read()
    assert header_problems(text, lang) == []


def test_the_polish_catalogs_have_the_polish_plural_forms():
    for path in catalog_paths("pl"):
        text = open(path, encoding="utf-8").read()
        assert "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);" in text


@pytest.mark.parametrize(
    "bad",
    [
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n',
        'msgid ""\nmsgstr ""\n"Language: de\\n"\n"Content-Type: text/plain; charset=UTF-8\\n"\n"Plural-Forms: nplurals=2; plural=(n != 1);\\n"\n',
        'msgid ""\nmsgstr ""\n"Language: pl\\n"\n"Content-Type: text/plain; charset=CHARSET\\n"\n"Plural-Forms: nplurals=3; plural=0;\\n"\n',
    ],
)
def test_a_wrong_header_is_found(bad):
    assert header_problems(bad, "pl")


def test_the_polish_catalog_translates_the_main_terms():
    entries = {m: s for m, s, _ in parse_po(read("po/pl.po"))}
    assert entries["Password"] == "Hasło"
    assert entries["Username"] == "Nazwa użytkownika"
    assert entries["Username:"] == "Nazwa użytkownika:"
    assert entries["_Cancel"] == "_Anuluj"


# ---- tests: the auth dialog ------------------------------------------------------------


def load_dialog():
    spec = importlib.util.spec_from_file_location(
        "auth_dialog", os.path.join(ROOT, "auth-dialog", "nm-gpclient-auth-dialog.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_auth_dialog_uses_the_catalog_of_the_package():
    text = read("auth-dialog/nm-gpclient-auth-dialog.py")
    assert re.search(
        rf'gettext\.translation\(\s*"{GNOME_DOMAIN}",\s*localedir="/usr/share/locale",\s*fallback=True', text
    )


@needs_gettext
def test_the_auth_dialog_shows_polish_text_with_the_catalog(tmp_path):
    import gettext

    mo = tmp_path / "pl.mo"
    subprocess.run(["msgfmt", "-o", str(mo), os.path.join(ROOT, "po", "pl.po")], check=True)
    with open(mo, "rb") as handle:
        module = load_dialog()
        module._ = gettext.GNUTranslations(handle).gettext
    assert module.secret_label("password") == "Hasło"
    assert module.header_text("Praca", None, False) == "Połączenie z „Praca” wymaga danych logowania."
    assert module.header_text("Praca", None, True).startswith("Uwierzytelnianie nie powiodło się.")


def test_the_auth_dialog_stays_english_without_a_catalog(monkeypatch):
    # an installed catalog in the developer's locale must not change the result
    monkeypatch.setenv("LANGUAGE", "C")
    monkeypatch.setenv("LC_ALL", "C")
    module = load_dialog()
    assert module.secret_label("password") == "Password"
    assert module.secret_label("some-key") == "Some key"  # a key of the service has no text
    assert module.header_text("Work", None, False) == "Credentials are required to connect to “Work”."
    assert module.header_text("Work", "From the service", False) == "From the service"


# ---- tests: packaging ------------------------------------------------------------------


def rules_problems(text):
    problems = []
    if "$(MAKE) mo" not in text:
        problems.append("rules do not compile the catalogs")
    if f"install-mo MO_DOMAIN={GNOME_DOMAIN} DESTDIR=$(CURDIR)/debian/network-manager-gpclient\n" not in text:
        problems.append("the core package gets no NetworkManager-gpclient.mo")
    if f"install-mo MO_DOMAIN={PLASMA_DOMAIN} DESTDIR=$(CURDIR)/debian/$(PLASMA_PACKAGE)\n" not in text:
        problems.append("the Plasma package gets no plasmanetworkmanagement_gpclientui.mo")
    return problems


def spec_problems(text):
    problems = []
    for needle in (
        "BuildRequires:  gettext",
        f"make install-mo MO_DOMAIN={GNOME_DOMAIN} DESTDIR=%{{buildroot}}",
        f"make install-mo MO_DOMAIN={PLASMA_DOMAIN} DESTDIR=%{{buildroot}}",
        f"%find_lang {GNOME_DOMAIN}",
        f"%find_lang {PLASMA_DOMAIN}",
        f"%files -f {GNOME_DOMAIN}.lang",
        f"%files plasma -f {PLASMA_DOMAIN}.lang",
    ):
        if needle not in text:
            problems.append(f"spec lacks: {needle}")
    return problems


def test_debian_rules_install_both_catalogs():
    assert rules_problems(read("debian/rules")) == []


def inside_core_block(rules, needle):
    """Is the line with needle inside an ifneq ($(HAS_CORE),) block of the rules"""
    stack = []
    for line in rules.splitlines():
        word = line.split(" ")[0]
        if word.startswith(("ifneq", "ifeq", "ifdef", "ifndef")):
            stack.append("$(HAS_CORE)" in line)
        elif word == "endif":
            stack.pop()
        elif needle in line:
            return any(stack)
    raise AssertionError(needle)


def test_the_rules_build_and_install_the_plasma_catalog_for_every_variant():
    # The KDE neon variant has no core package (HAS_CORE is empty): the Plasma catalog is
    # built and installed outside the core blocks, into the package of the control file
    rules = read("debian/rules")
    assert not inside_core_block(rules, "$(MAKE) mo")
    assert not inside_core_block(rules, f"install-mo MO_DOMAIN={PLASMA_DOMAIN}")


def test_the_core_catalog_is_installed_with_the_core_package_only():
    assert inside_core_block(read("debian/rules"), f"install-mo MO_DOMAIN={GNOME_DOMAIN}")


def test_the_core_block_check_sees_a_catalog_in_the_wrong_place():
    assert inside_core_block("ifneq ($(HAS_CORE),)\n\tmake mo\nendif\n", "make mo")
    assert not inside_core_block("ifneq ($(HAS_CORE),)\nendif\n\tmake mo\n", "make mo")


@pytest.mark.parametrize(
    "old",
    [
        "$(MAKE) mo",
        f"install-mo MO_DOMAIN={GNOME_DOMAIN} DESTDIR=$(CURDIR)/debian/network-manager-gpclient",
        f"install-mo MO_DOMAIN={PLASMA_DOMAIN} DESTDIR=$(CURDIR)/debian/$(PLASMA_PACKAGE)",
    ],
)
def test_rules_without_an_install_step_fail(old):
    text = read("debian/rules")
    assert old in text
    assert rules_problems(text.replace(old, "true"))


def test_the_spec_installs_both_catalogs():
    assert spec_problems(read("rpm/network-manager-gpclient.spec")) == []


@pytest.mark.parametrize("needle", ["BuildRequires:  gettext", "%find_lang " + GNOME_DOMAIN, "%files -f "])
def test_a_spec_without_the_catalog_steps_fails(needle):
    text = read("rpm/network-manager-gpclient.spec")
    assert needle in text
    assert spec_problems(text.replace(needle, "# removed"))


CONTROLS = sorted(glob.glob(os.path.join(ROOT, "debian", "control*")))


@pytest.mark.parametrize("path", CONTROLS, ids=os.path.basename)
def test_every_control_file_build_depends_on_gettext(path):
    assert re.search(r"(^|[\s,|])gettext\s*(,|\(|$)", build_depends(read(os.path.relpath(path, ROOT))), re.M)


@pytest.mark.parametrize(
    "text",
    [
        "Source: x\nBuild-Depends: debhelper-compat (= 13),\n               cmake,\nStandards-Version: 4\n",
        "Source: x\nBuild-Depends: debhelper-compat (= 13),\n               gettext-base,\nStandards-Version: 4\n",
        "Source: x\nStandards-Version: 4\nDepends: gettext,\n",
    ],
)
def test_a_control_file_without_gettext_fails(text):
    assert not re.search(r"(^|[\s,|])gettext\s*(,|\(|$)", build_depends(text), re.M)


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(ROOT, "Dockerfile.ubuntu*"))), ids=os.path.basename)
def test_every_build_image_has_gettext(path):
    assert re.search(r"^\s+gettext \\$", read(os.path.relpath(path, ROOT)), re.M)


def catalog_check_problems(text):
    """The RPM install test checks both catalogs in the package contents: on
    disk they may be missing (%lang files and %_install_langs)"""
    problems = []
    for package, domain in (
        ("network-manager-gpclient", GNOME_DOMAIN),
        ("network-manager-gpclient-plasma", PLASMA_DOMAIN),
    ):
        path = f"/usr/share/locale/pl/LC_MESSAGES/{domain}.mo"
        if not re.search(rf"rpm -ql {package} \\?\s*\|\s*grep -qx {re.escape(path)}", text):
            problems.append(f"{domain}.mo is not checked in the contents of {package}")
        if re.search(rf"^\s+{re.escape(path)} \\$", text, re.M):
            problems.append(f"{domain}.mo is checked on disk")
    return problems


def test_the_install_test_checks_both_catalogs_of_the_rpms():
    assert catalog_check_problems(read("rpm/install-test.sh")) == []


def test_a_catalog_checked_on_disk_is_reported():
    text = read("rpm/install-test.sh").replace(
        '    [ -e "$path" ] || fail "missing file $path"',
        f"    /usr/share/locale/pl/LC_MESSAGES/{GNOME_DOMAIN}.mo \\\n"
        '    [ -e "$path" ] || fail "missing file $path"',
    )
    assert catalog_check_problems(text)


def make_install_mo(tmp_path, *args):
    return subprocess.run(
        ["make", "-C", ROOT, "install-mo", f"DESTDIR={tmp_path}/root", f"LOCALE_BUILD={tmp_path}/build", *args],
        capture_output=True,
        text=True,
    )


@needs_gettext
@pytest.mark.skipif(not shutil.which("make"), reason="make is not installed")
@pytest.mark.parametrize("domain", [GNOME_DOMAIN, PLASMA_DOMAIN])
def test_install_mo_installs_one_domain_for_every_language(tmp_path, domain):
    result = make_install_mo(tmp_path, f"MO_DOMAIN={domain}")
    assert result.returncode == 0, result.stderr
    installed = sorted(
        os.path.relpath(os.path.join(d, f), tmp_path / "root")
        for d, _, files in os.walk(tmp_path / "root")
        for f in files
    )
    assert installed == [f"usr/share/locale/{lang}/LC_MESSAGES/{domain}.mo" for lang in languages()]


@needs_gettext
@pytest.mark.skipif(not shutil.which("make"), reason="make is not installed")
def test_install_mo_without_a_domain_installs_nothing(tmp_path):
    result = make_install_mo(tmp_path)
    assert result.returncode != 0
    assert "MO_DOMAIN" in result.stderr
    assert not (tmp_path / "root").exists()


def test_make_pot_runs_the_update_script():
    assert re.search(r"^pot:\n\tpython3 scripts/update_pot\.py$", read("Makefile"), re.M)
    assert os.access(os.path.join(ROOT, "scripts", "update_pot.py"), os.X_OK)
    assert sys.version_info >= (3, 10)  # the script uses only the standard library


def test_the_kde_extraction_script_names_the_plasma_domain():
    text = read("plugins/plasma/Messages.sh")
    assert text.startswith("#!/bin/sh\n")
    assert f"/{PLASMA_DOMAIN}.pot" in text
    assert "plugins/plasma/Messages.sh" in read("Makefile")  # shellcheck covers it


def plasma_browser_problems(text):
    """The Plasma editor must treat its "Custom..." entry as no browser, in
    isValid() as in setting() (a translated label is no valid browser name)"""
    problems = []
    valid = text[text.index("bool GpclientWidget::isValid()"):]
    valid = valid[: valid.index("\n}\n")]
    if "browserValue(m_ui->browserComboBox->currentText())" not in valid:
        problems.append("isValid() validates the raw browser combo text")
    if text.count("browserValue(m_ui->browserComboBox->currentText())") < 2:
        problems.append("setting() and isValid() do not share browserValue()")
    return problems


def test_the_plasma_custom_browser_entry_is_no_browser():
    assert plasma_browser_problems(read("plugins/plasma/gpclientwidget.cpp")) == []


def test_validating_the_raw_combo_text_is_reported():
    text = read("plugins/plasma/gpclientwidget.cpp").replace(
        "browserValue(m_ui->browserComboBox->currentText()).toUtf8()",
        "m_ui->browserComboBox->currentText().toUtf8()",
    )
    assert plasma_browser_problems(text)
