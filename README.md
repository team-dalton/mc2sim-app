# mc2sim — Minecraft world → building model → evacuation simulation

**Open the app: https://taichiops.github.io/mc2sim-app/**

This repository holds only the built website. Everything runs in your browser: the Python code (in `py/`)
runs with [Pyodide](https://pyodide.org) in a Web Worker. The Minecraft world you pick (a world folder or a `.zip`
of it) is read on your own computer; the world, the building model and the simulation results are never uploaded
anywhere. Closing the tab discards them. The page loads Pyodide and three.js from jsDelivr (code only).

Try *the built-in demo building* if you have no world at hand. Results use literature default parameters and are
not a certified egress analysis.
