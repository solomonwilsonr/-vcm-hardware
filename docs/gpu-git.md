# GPU Git (scope only, not implemented)

**Status: planned. Nothing in this repository implements GPU Git.** This page records the intended boundary so it can be designed deliberately instead of growing inside VCM.

## What it is meant to be

A separate, specialised version-control module for GPU-centric artifacts used by GPUOS and VACE: very large, frequently changing binary and tensor-like assets (for example kernels, compiled shader or command-stream bundles, calibration tables, model or weight shards) where Git's text-oriented model, delta compression and whole-file transfer are a poor fit.

## Why separate from VCM Hardware

- Different data shape: large chunked binaries and structured tensors rather than design files with semantic structure.
- Different performance profile: chunk-level dedup, content-defined chunking, partial and lazy fetch, possibly GPU-aware hashing and verification.
- Different consumers: GPUOS and VACE runtimes rather than hardware engineers reviewing a board revision.

## Interoperability (intended, not built)

- Both modules use SHA-256 content addressing, so an artifact hash in a VCM release manifest could reference an object held by GPU Git.
- VCM would treat GPU Git objects as opaque artifacts (category `firmware` or `other`) pinned by hash; GPU Git would not need to understand KiCad or BOMs.
- A shared, minimal manifest reference format (hash, size, locator) would let a VCM release say "this firmware bundle is object X in GPU Git" and let verification re-check it.

## Out of scope for VCM

Chunking, transport, GPU hashing, tensor-aware diff, and any GPUOS/VACE runtime integration. The VCM object store streams large files but does no chunk-level deduplication.

## Open questions

Exact GPUOS/VACE artifact types, size ranges and update frequency; required transport (local, network, object storage); security and licensing constraints; whether verification must run on the GPU. These need input before any design is proposed.
