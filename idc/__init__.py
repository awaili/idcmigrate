"""idc-migrate: IDC → Tencent Cloud migration copilot.

A single core library (``idc.core``) shared by:
  * the ``idc`` CLI (``idc.cli``)
  * the FastAPI web backend (``idc.backend``)

AI layers:
  * ``idc.llm``  — local LLM gateway client (match explanation, RAG Q&A, summaries)
  * ``idc.agent`` — Claude Code CLI runner for agentic migration tasks
"""

__version__ = "0.1.0"