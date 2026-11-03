import contextlib
import importlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

INSTALL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, INSTALL)
preflight = importlib.import_module("preflight")
chart_architecture_error = preflight.chart_architecture_error
extra_values_architecture_error = preflight.extra_values_architecture_error

HELM_STUB = """#!/bin/sh
case "$1" in
  pull)
    while [ $# -gt 0 ]; do
      [ "$1" = --untardir ] && dir="$2"
      shift
    done
    mkdir -p "$dir/nexus-installer"
    printf '%s' "$STUB_SCHEMA" > "$dir/nexus-installer/values.schema.json"
    ;;
  *) echo "stub helm: $1 failed" >&2; exit 1 ;;
esac
"""


def example_inputs():
    with open(os.path.join(INSTALL, "customer.example.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def generate(architecture=None):
    inp = example_inputs()
    if architecture is not None:
        inp["architecture"] = architecture
    with tempfile.TemporaryDirectory() as d:
        inputs = os.path.join(d, "customer.yaml")
        with open(inputs, "w", encoding="utf-8") as f:
            yaml.safe_dump(inp, f)
        out = os.path.join(d, "generated")
        proc = subprocess.run(
            [sys.executable, os.path.join(INSTALL, "gen-values.py"), "-f", inputs, "-o", out],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return proc, None, None
        with open(os.path.join(out, "values.install.yaml"), encoding="utf-8") as f:
            values = yaml.safe_load(f)
        env = {}
        with open(os.path.join(out, "inputs.env"), encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    env[k] = shlex.split(v)[0] if v else ""
        return proc, values, env


def schema(global_properties):
    return {"properties": {"global": {"properties": global_properties}}}


class TempDirTest(unittest.TestCase):
    def tempdir(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def write(self, name, content):
        path = os.path.join(self.tempdir(), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def chart(self, global_properties):
        path = self.write("values.schema.json", json.dumps(schema(global_properties)))
        return os.path.dirname(path)


class GenValues(unittest.TestCase):
    def test_default_forwards_nothing(self):
        _, values, env = generate()
        self.assertNotIn("architecture", values["global"])
        self.assertEqual(env["ARCHITECTURE"], "")

    def test_ha_forwarded(self):
        _, values, env = generate("ha")
        self.assertEqual(values["global"]["architecture"], "ha")
        self.assertEqual(env["ARCHITECTURE"], "ha")

    def test_explicit_standalone_forwarded(self):
        _, values, _ = generate("standalone")
        self.assertEqual(values["global"]["architecture"], "standalone")

    def test_unknown_rejected(self):
        proc, _, _ = generate("cluster")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("architecture='cluster' is not supported", proc.stderr)


class CheckArchitecture(unittest.TestCase):
    def run_check(self, inp):
        preflight._fails = 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            preflight.check_architecture(inp)
        return preflight._fails, out.getvalue()

    def test_unset_passes(self):
        self.assertEqual(self.run_check({})[0], 0)

    def test_supported_values_pass(self):
        for a in ("standalone", "ha"):
            fails, out = self.run_check({"architecture": a})
            self.assertEqual(fails, 0)
            self.assertIn(f"architecture = {a}", out)

    def test_unknown_fails(self):
        fails, out = self.run_check({"architecture": "cluster"})
        self.assertEqual(fails, 1)
        self.assertIn("architecture='cluster' is not supported", out)


class ChartSchema(TempDirTest):
    def test_chart_without_key_refuses_ha(self):
        err = chart_architecture_error(self.chart({"sizing": {"type": "string"}}), "ha")
        self.assertIn("no global.architecture setting", err)

    def test_chart_without_ha_in_enum_refuses_ha(self):
        err = chart_architecture_error(self.chart({"architecture": {"enum": ["standalone"]}}), "ha")
        self.assertIn("does not accept global.architecture=ha", err)

    def test_chart_without_schema_refuses_ha(self):
        self.assertIn("cannot read", chart_architecture_error(self.tempdir(), "ha"))

    def test_chart_with_ha_accepts(self):
        d = self.chart({"architecture": {"type": "string", "enum": ["standalone", "ha"]}})
        self.assertIsNone(chart_architecture_error(d, "ha"))


class ExtraValues(TempDirTest):
    def test_architecture_refused(self):
        for body in ("global:\n  architecture: ha\n", "global:\n  architecture:\n"):
            err = extra_values_architecture_error(self.write("extra.yaml", body))
            self.assertIn("set `architecture` in customer.yaml instead", err)

    def test_other_keys_allowed(self):
        for body in ("global:\n  sizing: small\n", "nexus: {}\n", ""):
            self.assertIsNone(extra_values_architecture_error(self.write("extra.yaml", body)))


class InstallScript(TempDirTest):
    def install(self, architecture=None, chart_global=None, extra=None):
        d = self.tempdir()
        inp = example_inputs()
        inp["bundle"]["tag"] = "oci-stable-test"
        inp["staticIndex"]["id"] = "00000000-0000-4000-8000-000000000001"
        if architecture:
            inp["architecture"] = architecture
        inputs = os.path.join(d, "customer.yaml")
        with open(inputs, "w", encoding="utf-8") as f:
            f.write(yaml.safe_dump(inp).replace("acme", "zeta"))
        bindir = os.path.join(d, "bin")
        os.mkdir(bindir)
        helm = os.path.join(bindir, "helm")
        with open(helm, "w", encoding="utf-8") as f:
            f.write(HELM_STUB)
        os.chmod(helm, 0o755)
        args = [os.path.join(INSTALL, "install.sh"), "--dry-run", "-f", inputs]
        if extra is not None:
            args += ["--extra-values", self.write("extra.yaml", extra)]
        env = dict(os.environ,
                   PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
                   GEN_DIR=os.path.join(d, "generated"),
                   STUB_SCHEMA=json.dumps(schema(chart_global or {})))
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def test_ha_refused_by_chart_without_it(self):
        proc = self.install("ha", {"architecture": {"enum": ["standalone"]}})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not accept global.architecture=ha", proc.stderr)

    def test_ha_accepted_by_chart_with_it(self):
        proc = self.install("ha", {"architecture": {"enum": ["standalone", "ha"]}})
        self.assertIn("architecture=ha: chart 0.0.0-bundle.oci-stable-test declares it", proc.stderr)
        self.assertIn("chart render failed; see the error above", proc.stderr)

    def test_extra_values_architecture_refused(self):
        proc = self.install("ha", {"architecture": {"enum": ["standalone", "ha"]}},
                            extra="global:\n  architecture: standalone\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("set `architecture` in customer.yaml instead", proc.stderr)
        self.assertNotIn("declares it", proc.stderr)


if __name__ == "__main__":
    unittest.main()
