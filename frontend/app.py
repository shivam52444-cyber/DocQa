"""Streamlit UI. Stateless: everything durable lives in the backend."""
from __future__ import annotations

import os
import uuid

import streamlit as st

from api_client import BackendClient, BackendError

st.set_page_config(page_title="Document Q&A", page_icon="📄", layout="wide")

STATUS_ICON = {"processing": "⏳", "ready": "✅", "rejected": "🚫", "failed": "⚠️"}


@st.cache_resource
def get_client() -> BackendClient:
    return BackendClient(os.environ["BACKEND_URL"], os.getenv("INTERNAL_API_TOKEN"))


def init_state() -> None:
    ss = st.session_state
    if "session_id" not in ss:  # new browser tab / refresh => new session id
        ss.session_id = str(uuid.uuid4())
        ss.messages = []
        ss.sent_file_ids = set()
        ss.uploader_key = 0
        ss.flash = []


def on_upload_click() -> None:
    ss = st.session_state
    files = ss.get(f"uploader_{ss.uploader_key}") or []
    for f in files:
        if f.file_id in ss.sent_file_ids:
            continue
        try:
            get_client().upload(ss.session_id, f.name, f.getvalue(), f.type or "application/octet-stream")
            ss.sent_file_ids.add(f.file_id)
            ss.flash.append(("success", f"{f.name} uploaded, processing…"))
        except BackendError as exc:
            ss.flash.append(("error", f"{f.name}: {exc}"))
    ss.uploader_key += 1  # resets the file picker


def on_new_session() -> None:
    for key in ("session_id", "messages", "sent_file_ids", "uploader_key", "flash"):
        st.session_state.pop(key, None)


@st.fragment(run_every="3s")
def documents_panel() -> None:
    try:
        docs = get_client().list_documents(st.session_state.session_id)
    except BackendError as exc:
        st.warning(str(exc))
        return
    if not docs:
        st.caption("No documents yet.")
        return
    for d in docs:
        st.markdown(f"{STATUS_ICON.get(d['status'], '•')} **{d['filename']}** — {d['status']}")
        if d.get("reject_reason"):
            st.caption(d["reject_reason"])


def render_sources(sources: list[dict]) -> None:
    if not sources:
        return
    with st.expander("Sources"):
        for s in sources:
            page = f", page {s['page']}" if s.get("page") else ""
            st.markdown(f"**{s['filename']}**{page}")
            st.caption(s["snippet"])


init_state()
ss = st.session_state

with st.sidebar:
    st.header("Documents")
    for level, msg in ss.flash:
        getattr(st, level)(msg)
    ss.flash = []
    st.file_uploader(
        "Upload up to 5 PDF or TXT files", type=["pdf", "txt"], accept_multiple_files=True, key=f"uploader_{ss.uploader_key}"
    )
    st.button("Upload & process", on_click=on_upload_click, use_container_width=True)
    documents_panel()
    st.divider()
    st.button("Start new session", on_click=on_new_session, use_container_width=True)

st.title("📄 Ask your documents")

for m in ss.messages:
    with st.chat_message(m["role"]):
        (st.warning if m.get("status", "ok") not in ("ok", "no_documents") else st.markdown)(m["content"])
        render_sources(m.get("sources", []))

if prompt := st.chat_input("Ask a question about your documents", max_chars=2000):
    ss.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Thinking…"):
                res = get_client().ask(ss.session_id, prompt)
        except BackendError as exc:
            st.error(str(exc))
        else:
            if res["status"] in ("ok", "no_documents"):
                st.markdown(res["answer"])
            else:
                st.warning(res["answer"])
            render_sources(res["sources"])
            ss.messages.append(
                {"role": "assistant", "content": res["answer"], "status": res["status"], "sources": res["sources"]}
            )
