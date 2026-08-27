"""mac_assembly -- multi-agent assembly pipeline on top of MAC.

Reuses:
* the single-part MAC pipeline (``multi_agent_cad``) verbatim as the
  structural part generator;
* CAD Skills' ``cadpy.assembly.AssemblyHelper`` + positioning philosophy
  (mates as semantic relationships, fixed-first, named datums);
* MAC's LLM client, JSON retry, render views, token tracker, and the
  QA-Judge anti-hallucination pattern.

Entry point::

    python -m mac_assembly
"""


def __getattr__(name: str):
    # Lazy re-exports: keep `python -m mac_assembly._part_runner` light
    # (it must not pull langgraph / the assembly nodes).
    if name in ("build_assembly_graph", "get_initial_state", "main"):
        from mac_assembly import graph_assembly

        return getattr(graph_assembly, name)
    raise AttributeError(name)


__all__ = ["build_assembly_graph", "get_initial_state", "main"]
