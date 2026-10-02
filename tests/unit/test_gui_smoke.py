"""
Tests for the pure logic of the GUI smoke test of the upgrade test
(.github/scripts/gui_smoke_lib.py, used by gui_smoke_gtk.py, gui_smoke_plasma.py
and gui-smoke.sh). GTK, libnm and Qt are not needed: the probes only have to be
importable, and the widget tree they send to the checks is a plain dict.

Every positive test has negative counterparts: a missing field, a wrong text, a
zero-size widget, an ldd "not found", metadata of another service and a .name
file without a plugin are all reported.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import copy
import importlib
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(ROOT, ".github", "scripts")
sys.path.insert(0, SCRIPTS)
lib = importlib.import_module("gui_smoke_lib")
gui_smoke_gtk = importlib.import_module("gui_smoke_gtk")
gui_smoke_plasma = importlib.import_module("gui_smoke_plasma")

SERVICE = "org.freedesktop.NetworkManager.gpclient"
NAME_FILE = os.path.join(ROOT, "plugins", "gnome", "nm-gpclient-service.name")


def entry(text):
    return {"kind": "entry", "text": text}


def combo(items, text):
    return {"kind": "combo", "items": items, "text": text}


def good_tree():
    """What the GTK editor shows for lib.vpn_data(), in the shape the probe sends"""
    return {"kind": "other", "children": [
        {"kind": "label", "text": "Portal or gateway address:"},
        entry("vpn.example.com"),
        {"kind": "check", "text": "Address is a gateway (skip the portal)", "active": True,
         "children": [{"kind": "label", "text": "Address is a gateway (skip the portal)"}]},
        combo(["First proposed by portal (automatic)", "gw-a (a.example.com)", "gw-b (b.example.com)"],
              "gw-b (b.example.com)"),
        combo(["Browser (SAML, passkey, 2FA)", "Username and password (RSA token)"],
              "Username and password (RSA token)"),
        {"kind": "other", "children": [entry("test-user")]},
    ]}


class TestNameFile:
    def test_the_installed_file_has_the_service_and_the_plugin(self):
        with open(NAME_FILE, encoding="utf-8") as handle:
            parsed = lib.parse_name_file(handle.read())

        assert parsed == {"name": "GlobalProtect", "service": SERVICE, "plugin": "libnm-vpn-plugin-gpclient.so"}

    @pytest.mark.parametrize("text", [
        "",
        "[VPN Connection]\nname=GlobalProtect\nservice=x\n",  # no [libnm] plugin
        "[VPN Connection]\nname=GlobalProtect\nservice=x\n[libnm]\nplugin=\n",
        "[VPN Connection]\nname=GlobalProtect\n[libnm]\nplugin=libx.so\n",  # no service
        "[VPN Connection]\nservice=  \n[libnm]\nplugin=libx.so\n",
        "service=x\nplugin=y\n",  # not a keyfile
    ])
    def test_missing_service_or_plugin_is_refused(self, text):
        with pytest.raises(ValueError):
            lib.parse_name_file(text)

    def test_the_gui_probes_find_the_service_of_the_repository_file(self):
        # the vpn.data must not name another service than the file does
        with open(NAME_FILE, encoding="utf-8") as handle:
            assert lib.parse_name_file(handle.read())["service"] == SERVICE


class TestVpnData:
    def test_the_data_of_the_test_connection(self):
        data = lib.vpn_data()

        assert data["gateway"] == "vpn.example.com"
        assert data["preferred-gateway"] == "gw-b (b.example.com)"
        assert data["gateway-list"] == "gw-a (a.example.com);gw-b (b.example.com)"
        assert data["gateway-list-count"] == "2"

    def test_the_caller_cannot_change_the_expectations(self):
        lib.vpn_data()["gateway"] = "other.example.org"

        assert lib.vpn_data()["gateway"] == "vpn.example.com"

    def test_the_list_holds_the_expected_gateways(self):
        assert lib.GATEWAY_LIST.split(";") == lib.GATEWAYS


class TestEditorCheck:
    def test_the_good_tree_passes(self):
        assert lib.check_editor(good_tree(), 700, 300) == []

    def test_flatten_lists_every_descendant(self):
        kinds = [n["kind"] for n in lib.flatten(good_tree())]

        assert kinds.count("entry") == 2
        assert kinds.count("label") == 2

    @pytest.mark.parametrize("size", [(0, 300), (700, 0), (0, 0), (-1, 5), (None, 5)])
    def test_zero_size_is_reported(self, size):
        problems = lib.check_editor(good_tree(), *size)

        assert any("no size" in p for p in problems)

    def test_missing_gateway_entry(self):
        tree = good_tree()
        tree["children"][1] = entry("")

        problems = lib.check_editor(tree, 700, 300)

        assert problems == [f"no entry with the gateway {lib.GATEWAY!r}"]

    def test_wrong_gateway_text(self):
        tree = good_tree()
        tree["children"][1] = entry("vpn.example.org")

        assert any("gateway 'vpn.example.com'" in p for p in lib.check_editor(tree, 700, 300))

    def test_missing_username(self):
        tree = good_tree()
        tree["children"][5] = {"kind": "other", "children": [entry("")]}

        assert any("username" in p for p in lib.check_editor(tree, 700, 300))

    @pytest.mark.parametrize("items", [
        [],
        ["First proposed by portal (automatic)"],
        ["First proposed by portal (automatic)", "gw-a (a.example.com)"],  # one gateway missing
        ["First proposed by portal (automatic)", "gw-b (b.example.com)"],
    ])
    def test_gateway_list_without_both_gateways(self, items):
        tree = good_tree()
        tree["children"][3] = combo(items, "gw-b (b.example.com)")

        assert any("offers the gateways" in p for p in lib.check_editor(tree, 700, 300))

    def test_wrong_preferred_gateway(self):
        tree = good_tree()
        tree["children"][3]["text"] = "First proposed by portal (automatic)"

        problems = lib.check_editor(tree, 700, 300)

        assert len(problems) == 1 and "preferred gateway" in problems[0]

    def test_the_gateway_entry_alone_does_not_stand_in_for_the_combo(self):
        tree = good_tree()
        del tree["children"][3]

        assert any("offers the gateways" in p for p in lib.check_editor(tree, 700, 300))

    def test_wrong_authentication(self):
        tree = good_tree()
        tree["children"][4]["text"] = "Browser (SAML, passkey, 2FA)"

        problems = lib.check_editor(tree, 700, 300)

        assert len(problems) == 1 and "authentication" in problems[0]

    def test_missing_authentication_combo(self):
        tree = good_tree()
        del tree["children"][4]

        assert any("authentication modes" in p for p in lib.check_editor(tree, 700, 300))

    def test_check_button_not_ticked(self):
        tree = good_tree()
        tree["children"][2]["active"] = False

        assert any("not ticked" in p for p in lib.check_editor(tree, 700, 300))

    def test_check_button_missing(self):
        tree = good_tree()
        del tree["children"][2]

        assert any("no check button" in p for p in lib.check_editor(tree, 700, 300))

    def test_empty_tree(self):
        problems = lib.check_editor({"kind": "other"}, 700, 300)

        assert len(problems) == 5

    def test_all_problems_are_listed_together(self):
        tree = good_tree()
        tree["children"][1] = entry("x")
        tree["children"][4]["text"] = "y"

        assert len(lib.check_editor(tree, 0, 0)) == 3

    def test_the_input_is_not_changed(self):
        tree = good_tree()
        before = copy.deepcopy(tree)
        lib.check_editor(tree, 700, 300)

        assert tree == before


class TestEditorTextsMatchTheSources:
    """The expected texts are the ones the C sources put in the widgets"""

    @pytest.mark.parametrize("source", ["nm-gpclient-editor.c", "nm-gpclient-editor-gtk3.c"])
    def test_texts_exist_in_the_editor(self, source):
        with open(os.path.join(ROOT, "plugins", "gnome", source), encoding="utf-8") as handle:
            text = handle.read()

        for expected in lib.AUTH_ITEMS + [lib.AS_GATEWAY_LABEL]:
            assert f'"{expected}"' in text

    @pytest.mark.parametrize("source", ["nm-gpclient-editor.c", "nm-gpclient-editor-gtk3.c"])
    def test_vpn_data_keys_are_read_by_the_editor(self, source):
        with open(os.path.join(ROOT, "plugins", "gnome", source), encoding="utf-8") as handle:
            text = handle.read()

        for key in ("gateway", "as-gateway", "preferred-gateway", "gateway-list", "username", "auth-mode"):
            assert f'"{key}"' in text
            assert key in lib.vpn_data()


LDD_GOOD = """\
\tlinux-vdso.so.1 (0x00007ffd)
\tlibQt5Core.so.5 => /lib/x86_64-linux-gnu/libQt5Core.so.5 (0x00007f00)
\t/lib64/ld-linux-x86-64.so.2 (0x00007f01)
"""
LDD_BAD = LDD_GOOD + "\tlibKF5NetworkManagerQt.so.7 => not found\n\tlibfoo.so.1 => not found\n"


class TestLdd:
    def test_all_libraries_found(self):
        assert lib.parse_ldd(LDD_GOOD) == []
        assert lib.ldd_problems(0, LDD_GOOD) == []

    def test_not_found_is_listed(self):
        assert lib.parse_ldd(LDD_BAD) == ["libKF5NetworkManagerQt.so.7", "libfoo.so.1"]
        assert lib.ldd_problems(0, LDD_BAD) == [
            "library not found: libKF5NetworkManagerQt.so.7", "library not found: libfoo.so.1"]

    def test_a_failing_ldd_is_a_problem(self):
        problems = lib.ldd_problems(1, "ldd: ./x.so: No such file or directory")

        assert len(problems) == 1 and "status 1" in problems[0]

    def test_a_library_called_not_found_is_not_missing(self):
        assert lib.parse_ldd("\tlibnot.so => /usr/lib/not found.so (0x1)\n") == []


def plasma_meta(**changes):
    data = {
        "KPlugin": {"Id": "plasmanetworkmanagement_gpclientui", "ServiceTypes": ["PlasmaNetworkManagement/VpnUiPlugin"]},
        "X-NetworkManager-Services": SERVICE,
    }
    data.update(changes)
    return data


class TestPlasmaMetadata:
    def test_the_json_of_the_repository_passes(self):
        import json

        with open(os.path.join(ROOT, "plugins", "plasma", "plasmanetworkmanagement_gpclientui.json"), encoding="utf-8") as handle:
            data = json.load(handle)

        assert lib.check_plasma_metadata(data, SERVICE) == []

    def test_what_qpluginloader_returns_passes(self):
        loader = {"IID": "org.kde.plasma.networkmanagement.VpnUiPlugin", "className": "GpclientUiPlugin",
                  "MetaData": plasma_meta()}

        assert lib.check_plasma_metadata(loader, SERVICE) == []

    @pytest.mark.parametrize("meta", [
        {},
        None,
        "text",
        {"IID": "x", "className": "y"},  # a plugin without embedded JSON
        {"IID": "x", "MetaData": {}},
    ])
    def test_no_metadata(self, meta):
        assert lib.check_plasma_metadata(meta, SERVICE) == ["the plugin has no metadata"]

    def test_metadata_of_another_service(self):
        problems = lib.check_plasma_metadata(plasma_meta(**{"X-NetworkManager-Services": "org.example.other"}), SERVICE)

        assert len(problems) == 1 and "X-NetworkManager-Services" in problems[0]

    def test_a_service_list_may_name_ours(self):
        meta = plasma_meta(**{"X-NetworkManager-Services": f"org.example.other, {SERVICE}"})

        assert lib.check_plasma_metadata(meta, SERVICE) == []

    def test_a_service_that_only_contains_ours_is_not_ours(self):
        meta = plasma_meta(**{"X-NetworkManager-Services": SERVICE + "x"})

        assert lib.check_plasma_metadata(meta, SERVICE)

    def test_no_service_key(self):
        meta = plasma_meta()
        del meta["X-NetworkManager-Services"]

        assert any("X-NetworkManager-Services" in p for p in lib.check_plasma_metadata(meta, SERVICE))

    def test_wrong_plugin_id(self):
        meta = plasma_meta(KPlugin={"Id": "other", "ServiceTypes": ["PlasmaNetworkManagement/VpnUiPlugin"]})

        assert any("KPlugin.Id" in p for p in lib.check_plasma_metadata(meta, SERVICE))

    def test_wrong_service_type(self):
        meta = plasma_meta(KPlugin={"Id": "plasmanetworkmanagement_gpclientui", "ServiceTypes": ["Other/Type"]})

        assert any("ServiceTypes" in p for p in lib.check_plasma_metadata(meta, SERVICE))

    def test_kplugin_is_not_a_dict(self):
        assert len(lib.check_plasma_metadata(plasma_meta(KPlugin="x"), SERVICE)) == 2


class TestQtMajor:
    @pytest.mark.parametrize("path, major", [
        ("/usr/lib/x86_64-linux-gnu/qt5/plugins/plasma/network/vpn/p.so", 5),
        ("/usr/lib/aarch64-linux-gnu/qt6/plugins/plasma/network/vpn/p.so", 6),
    ])
    def test_known(self, path, major):
        assert lib.qt_major(path) == major

    @pytest.mark.parametrize("path", ["/usr/lib/p.so", "/usr/lib/qt4/p.so", "/usr/lib/qt55/p.so", ""])
    def test_unknown(self, path):
        assert lib.qt_major(path) is None


class TestScreenshot:
    def test_first_existing_tool_wins(self):
        found = {"import-im6.q16": "/usr/bin/import-im6.q16", "import-im7.q16": "/usr/bin/import-im7.q16"}

        assert lib.find_import_tool(found.get) == "/usr/bin/import-im7.q16"
        assert lib.find_import_tool({"import": "/usr/bin/import"}.get) == "/usr/bin/import"

    def test_no_tool(self):
        assert lib.find_import_tool(lambda name: None) is None

    def test_command(self):
        assert lib.screenshot_command("/usr/bin/import", ":99", "/out/a.png") == [
            "/usr/bin/import", "-display", ":99", "-window", "root", "/out/a.png"]

    @pytest.mark.parametrize("out", ["/out/a.jpg", "/out/a", ""])
    def test_only_png(self, out):
        with pytest.raises(ValueError):
            lib.screenshot_command("import", ":99", out)


class TestProbes:
    def run(self, module, argv, capsys):
        with pytest.raises(SystemExit) as stop:
            module.main(argv)
        return stop.value.code, capsys.readouterr().err

    def test_gtk_probe_needs_a_valid_name_file(self, tmp_path, capsys):
        bad = tmp_path / "x.name"
        bad.write_text("[VPN Connection]\nservice=x\n")

        code, err = self.run(gui_smoke_gtk, ["--gtk", "3", "--name-file", str(bad), "--screenshot", "/x.png"], capsys)

        assert code == 1 and "no plugin" in err

    def test_gtk_probe_refuses_a_missing_name_file(self, tmp_path, capsys):
        code, err = self.run(gui_smoke_gtk, ["--gtk", "4", "--name-file", str(tmp_path / "none"), "--screenshot", "/x.png"], capsys)

        assert code == 1 and "FAIL:" in err

    def test_gtk_probe_refuses_another_toolkit(self, capsys):
        code, _ = self.run(gui_smoke_gtk, ["--gtk", "2", "--name-file", NAME_FILE, "--screenshot", "/x.png"], capsys)

        assert code == 2

    def test_plasma_probe_refuses_a_missing_plugin(self, tmp_path, capsys):
        code, err = self.run(gui_smoke_plasma, ["--plugin", str(tmp_path / "none.so"), "--name-file", NAME_FILE], capsys)

        assert code == 1 and "does not exist" in err

    def test_plasma_probe_refuses_a_bad_name_file(self, tmp_path, capsys):
        bad = tmp_path / "x.name"
        bad.write_text("")

        code, err = self.run(gui_smoke_plasma, ["--plugin", str(tmp_path / "p.so"), "--name-file", str(bad)], capsys)

        assert code == 1 and "no service" in err
