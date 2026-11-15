"""
TokenSafe Docker & Kubernetes Output Reducer (v3.3.0).

Compacts verbose container logs, Docker build steps, and multi-resource Kubernetes YAML.
"""

import re
from typing import Tuple, Dict, Any, List
from src.stash import stash_block


class ContainerAndK8sReducer:
    """Folds container build logs and Kubernetes manifests."""

    @classmethod
    def reduce_k8s_yaml(cls, content: str) -> Tuple[str, bool]:
        """Summarizes multi-resource Kubernetes YAML manifests."""
        lines = content.splitlines()
        if len(lines) < 25 or "apiVersion:" not in content or "kind:" not in content:
            return content, False

        docs = re.split(r"^---\s*$", content, flags=re.MULTILINE)
        summaries = []

        for doc in docs:
            doc_s = doc.strip()
            if not doc_s:
                continue
            kind_m = re.search(r"^kind:\s*(\w+)", doc_s, re.MULTILINE)
            name_m = re.search(r"^\s*name:\s*([\w.-]+)", doc_s, re.MULTILINE)
            ns_m = re.search(r"^\s*namespace:\s*([\w.-]+)", doc_s, re.MULTILINE)
            image_m = re.findall(r"^\s*image:\s*([^\s]+)", doc_s, re.MULTILINE)
            replicas_m = re.search(r"^\s*replicas:\s*(\d+)", doc_s, re.MULTILINE)

            kind = kind_m.group(1) if kind_m else "Resource"
            name = name_m.group(1) if name_m else "unnamed"
            ns = f" ({ns_m.group(1)})" if ns_m else ""

            details = []
            if replicas_m:
                details.append(f"replicas={replicas_m.group(1)}")
            if image_m:
                details.append(f"images={','.join(image_m)}")

            det_str = f" [{', '.join(details)}]" if details else ""
            summaries.append(f"- **{kind}**: `{name}`{ns}{det_str}")

        if not summaries:
            return content, False

        hash8, marker = stash_block(content, source="k8s_manifest")
        out = [
            f"# [TokenSafe Kubernetes Manifest Summary: {len(summaries)} resources]",
            "",
            *summaries,
            "",
            marker
        ]
        return "\n".join(out), True

    @classmethod
    def reduce_docker_build(cls, content: str) -> Tuple[str, bool]:
        """Folds verbose Docker build logs, keeping step headers and errors."""
        lines = content.splitlines()
        if len(lines) < 25 or not any("STEP " in ln or "Step " in ln or "#" in ln for ln in lines[:10]):
            return content, False

        steps = []
        errors = []
        for ln in lines:
            s = ln.strip()
            if re.match(r"^(?:Step\s+\d+/\d+|#[0-9]+\s+\[)", s):
                steps.append(s)
            elif "ERROR:" in s or "failed to solve" in s or "Error response from daemon" in s:
                errors.append(s)

        if not steps and not errors:
            return content, False

        hash8, marker = stash_block(content, source="docker_build")
        out = [
            f"# [TokenSafe Docker Build Summary: {len(steps)} steps executed]",
            "",
            "## Steps:"
        ]
        for step in steps[-8:]:
            out.append(f"  {step}")
        if errors:
            out.append("")
            out.append("## Build Errors:")
            for err in errors:
                out.append(f"  ✗ {err}")
        out.append("")
        out.append(marker)
        return "\n".join(out), True
