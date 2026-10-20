#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniCut launcher.  Run:  python file.py   (the code lives in the minicut/ package)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from minicut.app import main

if __name__ == "__main__":
    main()
