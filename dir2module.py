"""
Recursively read RPMs from DIR or read them from specified pkglist
and generate N:S:V:C:A.modulemd.yaml for them.
"""

import os
import sys
import argparse
import fnmatch
import gi
import rpm
import hawkey
from dnf.subject import Subject


gi.require_version("Modulemd", "2.0")
from gi.repository import Modulemd


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
        return pkglist.read().split()


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


def package2nevra(package):
    """
    Takes a package filename and returns its NEVRA
    """
    subject = Subject(os.path.basename(package.strip(".rpm")))
    nevras = subject.get_nevra_possibilities(forms=[hawkey.FORM_NEVRA])
    for nevra in nevras:
        return "{N}-{E}:{V}-{R}.{A}".format(N=nevra.name, E=nevra.epoch or 0,
                                            V=nevra.version, R=nevra.release,
                                            A=nevra.arch)


def package_header(package):
    """
    Examine a RPM package file and return its headers
    See https://docs.fedoraproject.org/en-US/Fedora_Draft_Documentation/0.1/html/RPM_Guide/ch16s04.html
    """
    ts = rpm.TransactionSet()
    fd = os.open(package, os.O_RDONLY)
    h = ts.hdrFromFdno(fd)
    os.close(fd)
    return h


def package_license(package):
    """
    Examine a RPM package and return its license
    """
    header = package_header(package)
    return header["license"]


def package_has_modularity_label(package):
    """
    Examine a RPM package and see if it has `ModularityLabel` set in its header
    """
    header = package_header(package)
    return "ModularityLabel" in header


def dumps_modulemd(name, stream, version, context, summary, arch, description,
                   module_license, licenses, packages, requires):
    """
    Generate modulemd yaml based on input parameters and return it as a string
    """
    mod_stream = Modulemd.ModuleStreamV2.new(name, stream)
    mod_stream.set_version(version)
    mod_stream.set_context(context)
    mod_stream.set_summary(summary)
    mod_stream.set_description(description)

    mod_stream.add_module_license(module_license)
    for pkglicense in licenses:
        mod_stream.add_content_license(pkglicense)

    for package in package_names(packages):
        component = Modulemd.ComponentRpm.new(package)
        component.set_rationale("Present in the repository")
        mod_stream.add_component(component)
        mod_stream.add_rpm_api(package)

    for nevra in package_nevras(packages):
        mod_stream.add_rpm_artifact(nevra)

    dependencies = Modulemd.Dependencies()
    for depname, depstream in requires.items():
        dependencies.add_runtime_stream(depname, depstream)
    mod_stream.add_dependencies(dependencies)

    index = Modulemd.ModuleIndex.new()
    index.add_module_stream(mod_stream)
    return index.dump_to_string()


def dump_modulemd(name, stream, version, context, arch, summary, description,
                  module_license, licenses, packages, requires):
    """
    Generate modulemd yaml based on input parameters write it into file
    """

    filename = module_filename(name, stream, version, context, arch)
    yaml = dumps_modulemd(name, stream, version, context, arch, summary,
                          description, module_license, licenses, packages,
                          requires)
    with open(filename, "w") as moduleyaml:
        moduleyaml.write(yaml)


def module_filename(name, stream, version, context, arch):
    """
    Generate filename for a module yaml
    """
    return "{N}:{S}:{V}:{C}:{A}.modulemd.yaml".format(
        N=name, S=stream, V=version, C=context, A=arch)


def parse_nsvca(nsvca):
    """
    Take module name, stream, version, context and architecture in a N:S:V:C:A
    format and return them as a separate values.
    """
    if nsvca.count(":") != 4:
        raise AttributeError("N:S:V:C:A in unexpected format")
    split = nsvca.split(":")
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
    return dict([dep.split(":") for dep in deps])


def main():
    parser = get_arg_parser()
    args = parser.parse_args()

    name, stream, version, context, arch = parse_nsvca(args.nsvca)
    path = os.path.expanduser(args.dir)
    packages = find_packages(path)
    requires = parse_dependencies(args.requires)
    description = args.description \
        or "This module has been generated using {0} tool".format(parser.prog)
    licenses = {package_license(package) for package in packages}

    if not all([package_has_modularity_label(package) for package in packages])\
       and not args.force:
        raise KeyError("All packages needs to contain `ModularityLabel` header "
                       "To suppress this constraint, use `--force` parameter")

    yaml = dumps_modulemd(name, stream, version, context, arch, args.summary,
                          description, args.license, licenses,
                          packages, requires)
    print(yaml)



if __name__ == "__main__":
    try:
        main()
    except (KeyError, AttributeError) as ex:
        sys.stderr.write("Error: {0}\n".format(str(ex)))
        sys.exit(1)
