# Translations

The plugin has two catalogs. Each desktop uses its own standard mechanism.

| Catalog | Used by | Format | Files |
|---|---|---|---|
| `NetworkManager-gpclient` | GNOME editors (GTK3, GTK4), the "add VPN" list, the auth dialog | gettext | `po/NetworkManager-gpclient.pot`, `po/<lang>.po` |
| `plasmanetworkmanagement_gpclientui` | Plasma editor | ki18n | `plugins/plasma/po/plasmanetworkmanagement_gpclientui.pot`, `plugins/plasma/po/<lang>/plasmanetworkmanagement_gpclientui.po` |

`po/LINGUAS` lists the languages. Each language has a file in both catalogs.
The packages install the compiled files (`.mo`) in
`/usr/share/locale/<lang>/LC_MESSAGES/`: `NetworkManager-gpclient.mo` in the
core package, `plasmanetworkmanagement_gpclientui.mo` in the Plasma package.

## What is translated

User-visible text: labels, tooltips, placeholders, dialog titles, the
validation messages of the editors and the name of the plugin in the VPN list.
Not translated: `vpn.data` keys, D-Bus names, file paths, log messages and the
text that the service sends to NetworkManager.

## Add a language

1. Add the language code (for example `de`) to `po/LINGUAS`.
2. Make the two files from the templates:

   ```bash
   msginit --no-translator -l de -i po/NetworkManager-gpclient.pot -o po/de.po
   mkdir -p plugins/plasma/po/de
   msginit --no-translator -l de \
       -i plugins/plasma/po/plasmanetworkmanagement_gpclientui.pot \
       -o plugins/plasma/po/de/plasmanetworkmanagement_gpclientui.po
   ```

3. Translate every entry. Keep the placeholders (`%s`, `%d`, `%1`), the markup
   and the keyboard accelerators (`_` in the GNOME catalog) as in the original.
4. Check the files: `msgfmt -c --check-format -o /dev/null po/de.po`
   (and the same for the Plasma file).
5. Run `python3 -m pytest tests/unit/test_translations.py -q`.

## Change the source texts: `make pot`

After you add or change a user-visible text, run:

```bash
make pot
```

It writes the two `.pot` files again (`scripts/update_pot.py`) and merges them
into every `.po` file with `msgmerge`. Then translate the new entries and the
entries that `msgmerge` marks `fuzzy`. A new source file with translatable text
goes into `po/POTFILES.in`. Use `_()` (or `N_()` for a static table) in C and
Python, `i18n()` in the Plasma C++ code. `tests/unit/test_translations.py`
fails when a `.pot` file is out of date, a `.po` file is wrong or a text is not
marked for translation.

For KDE's own tools, `plugins/plasma/Messages.sh` is the standard extraction
script. `make pot` does not use it: it needs `extractrc`, so the strings of the
`.ui` file are read by `scripts/update_pot.py` instead.

## Test a language

Install the packages (or `make install-dev`, which installs the GNOME
catalog), then start the program with the language:

```bash
LANGUAGE=pl LC_ALL=pl_PL.UTF-8 nm-connection-editor
printf 'DONE\n' | LANGUAGE=pl python3 auth-dialog/nm-gpclient-auth-dialog.py -u x -n Test -s org.freedesktop.NetworkManager.gpclient -i -t password
# without GTK, the texts as NetworkManager's external UI gets them:
printf 'DONE\n' | LANGUAGE=pl python3 auth-dialog/nm-gpclient-auth-dialog.py -u x -n Test -s org.freedesktop.NetworkManager.gpclient -i -t password --external-ui-mode
```

The locale must exist on the system (`locale -a`). For the Plasma editor, start
the Plasma session or `systemsettings` with the same variables.

## Machine translations

The Polish catalogs (`pl`) are a machine translation. They carry the line
`# Machine translation, needs review by a native speaker.` at the top. Native
speakers are welcome to review them: remove the line when the review is done.
