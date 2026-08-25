# Dify Raw HTML Artifact Delivery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Emit complete validated S3 HTML as the frontend's existing fenced raw-HTML response and reject direct model-generated HTML.

**Architecture:** Keep Code Interpreter plus S3 as the single isolated HTML handoff. Extend the Dify proxy after artifact validation to load and validate HTML, suppress direct model HTML, and emit the authoritative document in bounded OpenAI SSE chunks while preserving existing handling for other file types.

**Tech Stack:** Python 3.12, FastAPI, boto3 S3, OpenAI-compatible SSE, `unittest`.

---

### Task 1: Lock down the response contract

**Files:**
- Modify: `dify-proxy/tests/test_html_artifacts.py`

1. Add a fake S3 `get_object` body containing a complete UTF-8 HTML document.
2. Add a failing test asserting validated HTML becomes exactly one `html` fence.
3. Add a failing test asserting an S3 document replaces a direct model fence.
4. Add a failing test asserting direct HTML is suppressed when no artifact exists.
5. Run `python3 -m unittest discover -s dify-proxy/tests -v` and confirm the new S3 emission tests fail before implementation.

### Task 2: Implement safe raw-HTML loading

**Files:**
- Modify: `dify-proxy/dify-server.py`
- Test: `dify-proxy/tests/test_html_artifacts.py`

1. Add a helper that reads only an already validated HTML object.
2. Enforce the recorded size, strict UTF-8, doctype, closing tag, and safe fence content.
3. Partition HTML from non-HTML artifacts and format HTML as the exact fenced contract.
4. Log the selected delivery source without logging HTML or S3 object contents.
5. Run the focused test suite and confirm buffered behavior passes.

### Task 3: Enforce precedence in streaming responses

**Files:**
- Modify: `dify-proxy/dify-server.py`
- Test: `dify-proxy/tests/test_html_artifacts.py`

1. Add a chunk-safe detector that passes normal text but captures direct `html` fences.
2. At stream completion, resolve validated artifacts before releasing the buffered tail.
3. Remove direct HTML in all cases; return an explicit error when no validated S3 HTML exists.
4. Emit final HTML in bounded `delta.content` chunks.
5. Reassemble test SSE chunks and assert byte-for-byte frontend content and no duplicate HTML.

### Task 4: Verify compatibility and document operation

**Files:**
- Modify: `dify-proxy/README.md`
- Modify: `dify-proxy/build_and_push.sh`
- Modify: `dify-proxy/deploy.sh`
- Modify: `dify-proxy/k8s/deployment.yaml`

1. Document the authoritative S3 and direct fallback rules.
2. Update the release tag from `v0.0.8` to `v0.0.9`.
3. Run unit tests, Python compilation, shell syntax checks, and `git diff --check`.
4. Review the targeted Dify diff and confirm unrelated dirty files remain untouched.

### Task 5: Build and deploy Dify proxy v0.0.9

**Files:**
- No additional source files.

1. Build and push `agentcore-dify-proxy:v0.0.9` for Linux AMD64.
2. Apply only the Dify deployment manifest.
3. Wait for the EKS rollout and verify the new pod is ready.
4. Inspect startup logs and the live source contract.
5. Leave OpenWebUI deployments and AgentCore Runtime versions unchanged.
