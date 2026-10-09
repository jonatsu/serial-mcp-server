"""Spec files shared by the spec tests."""

VALID_SPEC = """\
---
kind: serial-protocol
name: "Test Device"
---

# Test Device Protocol

## Overview

A test device spec.

## Commands

### Read Sensor

- **Write to**: `1234`
- **Format**: `[0x01]`
"""

MINIMAL_SPEC = """\
---
kind: serial-protocol
name: "Minimal"
---

# Minimal Spec
"""
