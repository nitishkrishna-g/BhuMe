# AI Transcripts

This folder contains the AI conversation transcripts used during the development
of the cadastral boundary correction pipeline. AI was used in two ways:

1. **Understanding the problem** — web-based research chats to study cadastral
   survey systems, georeferencing artifacts, and cross-correlation techniques
2. **Building the solution** — coding sessions for implementation, debugging,
   calibration, and visualization

## Transcript Files

| File | Purpose | Tool |
|---|---|---|
| [`claude_session.md`](./claude_session.md) | Primary development session — full implementation from research to submission | Claude Opus 4.6 (Antigravity) |

## Web Chat Share Links

- **Primary coding session (Claude)**: Used throughout for code generation, debugging, and iteration
- **Research on cross-correlation for geospatial alignment**: Studied FFT-based template matching,
  NCC normalization strategies, and multi-pass alignment approaches

## How AI Was Directed

The AI was used as a **pair-programming partner**, not an autopilot. Key areas where
I directed the approach and challenged AI suggestions:

1. **Approach selection** — rejected AI's initial suggestion of ML-based methods (SIFT/ORB,
   CNN segmentation) because 6+3 truth plots is far too few. Pushed for cross-correlation
   after reasoning about the problem structure (the error IS a translation).

2. **Confidence calibration** — questioned the initial single-signal confidence (NCC only)
   and pushed for multi-signal scoring after observing that large plots got unfairly low
   confidence. Led to the edge-pixel normalization fix.

3. **Two-pass alignment** — noticed that Malatavadi had many failed plots and asked why
   the search window was missing. This led to the global-shift-then-refine strategy.

4. **Restraint strategy** — cross-questioned the decision logic for flagging vs correcting,
   especially for near-zero-shift plots and tiny plots with unreliable xcorr.

5. **Visualization** — directed the creation of an interactive HTML viewer with
   before/after toggle, inspired by the BhuMe playground interface.
