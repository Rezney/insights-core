#!/usr/bin/env python
"""
This module runs insights and serializes the results into a directory. It is
configurable with a yaml manifest that specifies what to load, what to run,
and what to serialize. If a manifest isn't provided, a default one is used that
runs all datasources in ``insights.specs.Specs`` and
``insights.specs.default.DefaultSpecs`` and saves all datasources in
``insights.specs.Specs``.
"""
from __future__ import print_function
import argparse
import logging
import os
import sys
import tempfile
import yaml

from datetime import datetime

from insights import apply_configs, apply_default_enabled, dr
from insights.core import blacklist
from insights.core.serde import Hydration
from insights.util import fs
from insights.util.subproc import call

SAFE_ENV = {
    "PATH": os.path.pathsep.join([
        "/bin",
        "/usr/bin",
        "/sbin",
        "/usr/sbin",
        "/usr/share/Modules/bin",
    ]),
    "LC_ALL": "C",
}

if "LANG" in os.environ:
    SAFE_ENV["LANG"] = os.environ["LANG"]

log = logging.getLogger(__name__)

default_manifest = """
---
# version is for the format of this file, not its contents.
version: 0

client:
    context:
        class: insights.core.context.HostContext
        args:
            timeout: 10 # timeout in seconds for commands. Doesn't apply to files.

    # commands and files to ignore
    blacklist:
        files: []
        commands: []
        patterns: []
        keywords: []

    # Can be a list of dictionaries with name/enabled fields or a list of strings
    # where the string is the name and enabled is assumed to be true. Matching is
    # by prefix, and later entries override previous ones. Persistence for a
    # component is disabled by default.
    persist:
        - name: insights.specs.Specs
          enabled: true
plugins:
    # disable everything by default
    # defaults to false if not specified.
    default_component_enabled: false

    # packages and modules to load
    packages:
        - insights.specs.default

    # configuration of loaded components. names are prefixes, so any component with
    # a fully qualified name that starts with a key will get the associated
    # configuration applied. Can specify timeout, which will apply to command
    # datasources. Can specify metadata, which must be a dictionary and will be
    # merged with the components' default metadata.
    configs:
        - name: insights.specs.Specs
          enabled: true

        - name: insights.specs.default.DefaultSpecs
          enabled: true

        - name: insights.parsers.hostname
          enabled: true

        - name: insights.parsers.facter
          enabled: true

        - name: insights.parsers.systemid
          enabled: true

        - name: insights.combiners.hostname
          enabled: true

    # needed because some specs aren't given names before they're used in DefaultSpecs
        - name: insights.core.spec_factory
          enabled: true
""".strip()


def load_manifest(data):
    """ Helper for loading a manifest yaml doc. """
    if isinstance(data, dict):
        return data
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict):
        raise Exception("Manifest didn't result in dict.")
    return doc


def load_packages(pkgs):
    for p in pkgs:
        dr.load_components(p, continue_on_error=False)


def apply_blacklist(cfg):
    for b in cfg.get("files", []):
        blacklist.add_file(b)

    for b in cfg.get("commands", []):
        blacklist.add_command(b)

    for b in cfg.get("patterns", []):
        blacklist.add_pattern(b)

    for b in cfg.get("keywords", []):
        blacklist.add_keyword(b)


def create_context(ctx):
    """
    Loads and constructs the specified context with the specified arguments.
    If a '.' isn't in the class name, the 'insights.core.context' package is
    assumed.
    """
    ctx_cls_name = ctx.get("class", "insights.core.context.HostContext")
    if "." not in ctx_cls_name:
        ctx_cls_name = "insights.core.context." + ctx_cls_name
    ctx_cls = dr.get_component(ctx_cls_name)
    ctx_args = ctx.get("args", {})
    return ctx_cls(**ctx_args)


def get_to_persist(persisters):
    """
    Given a specification of what to persist, generates the corresponding set
    of components.
    """
    def specs():
        for p in persisters:
            if isinstance(p, dict):
                yield p["name"], p.get("enabled", True)
            else:
                yield p, True

    components = sorted(dr.DELEGATES, key=dr.get_name)
    names = dict((c, dr.get_name(c)) for c in components)

    results = set()
    for p, e in specs():
        for c in components:
            if names[c].startswith(p):
                if e:
                    results.add(c)
                elif c in results:
                    results.remove(c)
    return results


def create_archive(path, remove_path=True):
    """
    Creates a tar.gz of the path using the path basename + "tar.gz"
    The resulting file is in the parent directory of the original path, and
    the original path is removed.
    """
    root_path = os.path.dirname(path)
    relative_path = os.path.basename(path)
    archive_path = path + ".tar.gz"

    cmd = [["tar", "-C", root_path, "-czf", archive_path, relative_path]]
    call(cmd, env=SAFE_ENV)
    if remove_path:
        fs.remove(path)
    return archive_path


def collect(manifest=default_manifest, tmp_path=None, compress=False):
    """
    This is the collection entry point. It accepts a manifest, a temporary
    directory in which to store output, and a boolean for optional compression.

    Args:
        manifest (str or dict): json document or dictionary containing the
            collection manifest. See default_manifest for an example.
        tmp_path (str): The temporary directory that will be used to create a
            working directory for storing component output as well as the final
            tar.gz if one is generated.
        compress (boolean): True to create a tar.gz and remove the original
            workspace containing output. False to leave the workspace without
            creating a tar.gz

    Returns:
        The full path to the created tar.gz or workspace.
    """

    manifest = load_manifest(manifest)
    client = manifest.get("client", {})
    plugins = manifest.get("plugins", {})

    apply_default_enabled(plugins.get("default_component_enabled", False))
    load_packages(plugins.get("packages", []))
    apply_blacklist(client.get("blacklist", {}))
    apply_configs(plugins)
    to_persist = get_to_persist(client.get("persist", set()))

    hostname = call("hostname -f", env=SAFE_ENV).strip()
    suffix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    relative_path = "insights-%s-%s" % (hostname, suffix)
    tmp_path = tmp_path or tempfile.gettempdir()
    output_path = os.path.join(tmp_path, relative_path)
    fs.ensure_path(output_path)
    fs.touch(os.path.join(output_path, "insights_archive.txt"))

    broker = dr.Broker()
    ctx = create_context(client.get("context", {}))
    broker[ctx.__class__] = ctx

    h = Hydration(output_path)
    broker.add_observer(h.make_persister(to_persist))
    list(dr.run_incremental(broker=broker))

    if compress:
        return create_archive(output_path)
    return output_path


def main(argv=sys.argv):
    p = argparse.ArgumentParser()
    p.add_argument("-m", "--manifest", help="Manifest yaml.")
    p.add_argument("-o", "--out_path", help="Path to write output data.")
    p.add_argument("-q", "--quiet", help="Error output only.", action="store_true")
    p.add_argument("-v", "--verbose", help="Verbose output.", action="store_true")
    p.add_argument("-d", "--debug", help="Debug output.", action="store_true")
    args = p.parse_args(argv)

    level = logging.WARNING
    if args.verbose:
        level = logging.INFO
    if args.debug:
        level = logging.DEBUG
    if args.quiet:
        level = logging.ERROR

    logging.basicConfig(level=level)

    if args.manifest:
        with open(args.manifest) as f:
            manifest = f.read()
    else:
        manifest = default_manifest

    out_path = args.out_path or tempfile.gettempdir()
    archive = collect(manifest, out_path, compress=True)
    print(archive)


if __name__ == "__main__":
    main()
