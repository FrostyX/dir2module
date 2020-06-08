#!/usr/bin/python3

"""
Recursively read RPMs from DIR or read them from specified pkglist
and generate N:S:V:C:A.modulemd.yaml for them.
"""

import argparse
import fnmatch
import os
import sys

import gi
import hawkey
import rpm
from dnf.subject import Subject


gi.require_version("Modulemd", "2.0")
from gi.repository import Modulemd


class Module(object):
    """
    Provide a high-level interface for representing modules and yaml generation
    based on their values.
    """
    def __init__(self, name, stream, version, context, arch, summary,
                 description, module_license, licenses, packages, requires):
        self.name = name
        self.stream = stream
        self.version = version
        self.context = context
        self.arch = arch
        self.summary = summary
        self.description = description
        self.module_license = module_license
        self.licenses = licenses
        self.packages = packages
        self.requires = requires

    @property
    def filename(self):
        """
        Generate filename for a module yaml
        """
        return "{N}:{S}:{V}:{C}:{A}.modulemd.yaml".format(
            N=self.name, S=self.stream, V=self.version,
            C=self.context, A=self.arch)

    def dumps(self):
        """
        Generate modulemd yaml based on input parameters and return it as a string
        """
        mod_stream = Modulemd.ModuleStreamV2.new(self.name, self.stream)
        mod_stream.set_version(self.version)
        mod_stream.set_context(self.context)
        mod_stream.set_summary(self.summary)
        mod_stream.set_description(self.description)

        mod_stream.add_module_license(self.module_license)
        for pkglicense in self.licenses:
            mod_stream.add_content_license(pkglicense)

        for nevra in package_nevras(self.packages):
            mod_stream.add_rpm_artifact(nevra)

        dependencies = Modulemd.Dependencies()
        for depname, depstream in self.requires.items():
            dependencies.add_runtime_stream(depname, depstream)
        mod_stream.add_dependencies(dependencies)

        index = Modulemd.ModuleIndex.new()
        index.add_module_stream(mod_stream)
        return index.dump_to_string()

    def dump(self):
        """
        Generate modulemd yaml based on input parameters write it into file
        """
        with open(self.filename, "w") as moduleyaml:
            moduleyaml.write(self.dumps())


def find_packages(path):
    """
    Recursively find RPM packages in a `path` and return their list
    """
    packages = []
    for root, _, filenames in os.walk(path):
        for filename in fnmatch.filter(filenames, "*.rpm"):
            if filename.endswith(".src.rpm"):
                continue
            packages.append(os.path.join(root, filename))
    return packages


def find_packages_in_file(path):
    """
    Parse a text file containing a list of packages and return their list
    """
    with open(path, "r") as pkglist:
        return pkglist.read().splitlines()


def package_names(packages):
    """
    Takes a list of package filenames and returns a set of unique package names
    """
    names = set()
    for package in packages:
        subject = Subject(os.path.basename(package.strip(".rpm")))
        nevras = subject.get_nevra_possibilities(forms=[hawkey.FORM_NEVRA])
        for nevra in nevras:
            names.add(nevra.name)
    return names


def package_nevras(packages):
    """
    Takes a list of package filenames and returns a set of unique NEVRAs
    """
    return {package2nevra(package) for package in packages}


def package2nevra(path):
    """
    Takes a package filename and returns its NEVRA
    """
    file_name = os.path.basename(path)
    if not file_name.endswith(".rpm"):
        raise ValueError("File name doesn't end with '.rpm': {}".format(path))
    # TODO: construct NEVRA from rpm header
    subject = Subject(file_name)
    nevras = subject.get_nevra_possibilities(forms=[hawkey.FORM_NEVRA])
    for nevra in nevras:
        return "{N}-{E}:{V}-{R}.{A}".format(N=nevra.name, E=nevra.epoch or 0,
                                            V=nevra.version, R=nevra.release,
                                            A=nevra.arch)


def package_header(path):
    """
    Examine a RPM package file and return its header
    See https://docs.fedoraproject.org/en-US/Fedora_Draft_Documentation/0.1/html/RPM_Guide/ch16s04.html
    """
    ts = rpm.TransactionSet()
    ts.setKeyring(rpm.keyring())
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES|rpm._RPMVSF_NODIGESTS)
    with open(path, "r") as f:
        hdr = ts.hdrFromFdno(f.fileno())
        return hdr


def package_license(package):
    """
    Examine a RPM package and return its license
    """
    # TODO: wrap in a class or pass hdr argument to avoid multiple reads
    header = package_header(package)
    return header["license"]


def package_has_modularity_label(package):
    """
    Examine a RPM package and see if it has `ModularityLabel` set in its header
    """
    # TODO: wrap in a class or pass hdr argument to avoid multiple reads
    header = package_header(package)
    return bool(header["modularitylabel"])


def parse_nsvca(nsvca):
    """
    Take module name, stream, version, context and architecture in a N:S:V:C:A
    format and return them as a separate values.
    """
    split = nsvca.split(":")
    if len(split) != 5:
        raise ValueError("N:S:V:C:A in unexpected format")
    split[2] = int(split[2])
    return split


def get_arg_parser():
    description = (
        "Recursively read RPMs from DIR or read them from specified pkglist."
        "If any RPM is missing on unreadable, error out."
        "Populate artifacts/rpms with RPM NEVRAs."
        "Populate license/content with list of RPM licenses."

        "Write N:S:V:C:A.modulemd.yaml in the current directory."
        "Make sure the yaml is in modulemd v2 format."
    )
    parser = argparse.ArgumentParser("dir2module", description=description)
    parser.add_argument("nsvca",
                        help=("Module name, stream version, context and "
                              "architecture in a N:S:V:C:A format"))
    parser.add_argument("-m", "--summary", required=True, help="Module summary")
    parser.add_argument("-d", "--description", help="Module description")
    parser.add_argument("-l", "--license", default="MIT", help="Module license")
    parser.add_argument("-r", "--requires", action="append",
                        help=("Module runtime dependencies in a N:S format. "
                              "For multiple dependencies, repeat this option"))
    parser.add_argument("--force", action="store_true",
                        help="Suppress all constraints and hope for the best")

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--dir", help="")
    input_group.add_argument("--pkglist", help="")
    return parser


def parse_dependencies(deps):
    if deps is None:
        return {}
    return dict([dep.split(":") for dep in deps])


def main():
    parser = get_arg_parser()
    args = parser.parse_args()
    name, stream, version, context, arch = parse_nsvca(args.nsvca)

    if args.dir:
        path = os.path.expanduser(args.dir)
        packages = find_packages(path)
    else:
        path = os.path.expanduser(args.pkglist)
        packages = find_packages_in_file(path)

    requires = parse_dependencies(args.requires)
    description = args.description \
        or "This module has been generated using {0} tool".format(parser.prog)
    licenses = {package_license(package) for package in packages}


    missing_labels = []
    for package in packages:
        if not package_has_modularity_label(package):
            missing_labels.append(package)
            msg = "ERROR: " if args.force else "WARNING: "
            msg += "RPM does not have `modularitylabel` header set: {}".format(package)
            print(msg)
    if missing_labels and not args.force:
        raise RuntimeError("All packages need to contain the `modularitylabel` header. "
                       "To suppress this constraint, use `--force` parameter")

    module = Module(name, stream, version, context, arch, args.summary,
                          description, args.license, licenses,
                          packages, requires)

    yaml = module.dumps()
    print(yaml)



if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as ex:
        sys.stderr.write("Error: {0}\n".format(str(ex)))
        sys.exit(1)
