# Jevflow

Keeps Claude accountable on long tasks: it plans work as phases, a judge (Jev by default) weighs every stop, deterministic checks decide, and several agents can share one plan.

This folder is the plugin itself, the only part of the repository that is installed. Documentation, design notes, tests, examples and media live in the repository root:

- Overview and setup: https://github.com/Parth1811/JevFlow#readme
- Reference: https://github.com/Parth1811/JevFlow/blob/main/docs/REFERENCE.md

## Install

```sh
claude plugin marketplace add Parth1811/JevFlow
claude plugin install jevflow@jevflow
```

Claude Code asks for your Jev API key when you enable the plugin (`/plugin`, pick **jevflow**, **Configure** to change it later). The key is kept in your system's secure credential store and passed only to Jevflow's hooks. `JEV_API_KEY` in your environment also works (the option wins when both are set). Other judges' keys come only from the plugin's `judge_api_key` option. Keys are never read from a file. Leave it empty to run on your checks only.

Requires Python 3.10+ on the PATH Claude gives hooks (macOS's own `python3` is 3.9; set `JEVFLOW_PYTHON` if needed).

## License

[MIT](LICENSE)
