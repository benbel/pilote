import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pilote

CHECKS = [
    ("list", "", "pods"),
    ("list", "", "secrets"),
    ("delete", "", "secrets"),
    ("delete", "", "services"),
    ("delete", "", "configmaps"),
    ("delete", "", "persistentvolumeclaims"),
    ("delete", "", "serviceaccounts"),
    ("delete", "apps", "deployments"),
    ("delete", "apps", "statefulsets"),
    ("delete", "networking.k8s.io", "ingresses"),
    ("delete", "networking.k8s.io", "networkpolicies"),
    ("delete", "rbac.authorization.k8s.io", "rolebindings"),
    ("delete", "rbac.authorization.k8s.io", "roles"),
    ]


def check_rights(kube):
    print("== Droits dans", kube.namespace)
    for verb, group, resource in CHECKS:
        review = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "namespace": kube.namespace,
                    "verb": verb,
                    "group": group,
                    "resource": resource,
                    },
                },
            }
        code, answer = kube.call(
            "POST",
            "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
            review,
            )
        allowed = "erreur %s" % code
        if code == 201:
            allowed = answer["status"]["allowed"]
        print("  %-6s %-45s %s" % (verb, group + "/" + resource, allowed))


def onyxia_record(kube, name):
    code, answer = kube.call(
        "GET",
        "/api/v1/namespaces/%s/secrets/sh.onyxia.release.v1.%s" % (kube.namespace, name),
        )
    if code != 200:
        return {}
    record = {}
    for key, value in (answer.get("data") or {}).items():
        if key in ("friendlyName", "catalog", "owner"):
            record[key] = base64.b64decode(value).decode(errors="replace")
    return record


def check_releases(kube):
    print("== Releases helm")
    for name, secret in kube.latest_releases().items():
        release = pilote.decode_release(secret)
        if release is None:
            print(" ", name, ": format non reconnu")
            continue
        config = release.get("config") or {}
        info = release.get("info") or {}
        kinds = []
        for api, kind, resource in pilote.parse_manifest(release.get("manifest", "")):
            kinds.append(kind)
        print(" ", name)
        print("     statut :", info.get("status"))
        print("     first_deployed :", info.get("first_deployed"))
        print("     repo :", pilote.section(config, "git").get("repository"))
        print("     hostname :", pilote.section(config, "ingress").get("hostname"))
        print("     mot de passe présent :", bool(pilote.section(config, "security").get("password")))
        print("     ressources :", ", ".join(sorted(set(kinds))))
        print("     clés de config :", sorted(config))
        print("     clés de config.git :", sorted(pilote.section(config, "git")))
        print("     onyxia :", onyxia_record(kube, name))


def main():
    auth = pilote.Auth()
    auth.token()
    kube = pilote.Kube(auth)
    check_rights(kube)
    check_releases(kube)


main()
