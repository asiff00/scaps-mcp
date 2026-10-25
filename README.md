# scaps-mcp

MCP server for SCAPS-1D, the one-dimensional solar cell simulator of the University of Gent. It drives your own SCAPS installation headless and gives any MCP client device design, simulation, batches, curve fitting, result analysis and realism checks.

SCAPS is not included. Request it from its authors as described on the [SCAPS website](https://scaps.elis.ugent.be/) and follow their conditions of use, which include citing SCAPS in publications.

## Capabilities

- Devices: describe any `.def`, edit it with every SCAPS `set` command, load `.material` files into layers, create devices from scratch (up to 7 layers, 7 defects per layer, 3 defects per interface), and set solver numerics that scripts cannot reach.
- Simulation: J-V, QE, C-V and C-f with any working point, illumination side, spectrum or generation model, ND filter and cut-off; energy band, generation-recombination and AC profiles.
- Batches: grid or paired sweeps over any `set` or `action` key, including temperature, intensity and bias.
- Fitting and optimisation: bounded Nelder-Mead against one or several measured curves (light and dark J-V, QE, C-V), or towards maximum, minimum or target efficiency, Voc, Jsc or FF.
- Input files: import shared `.def`, `.material`, absorption, spectrum, generation, filter, grading and measurement files, or write them from data with unit conversion (absorption from alpha or extinction coefficient k).
- Analysis: Shockley-Queisser limit of the absorber under the spectrum used, QE-integrated Jsc, Mott-Schottky built-in voltage and doping, band offsets at every interface.
- Realism checks: results above the detailed-balance limit, missing radiative recombination, ideal interfaces, band spikes and cliffs, compensating defects, implausible densities of states, default absorption models and other common causes of unphysical SCAPS studies.
- Jobs: long runs continue in the background with progress, waiting and cancellation.
- Validation: every key and value is checked against the command list of the installed SCAPS version before SCAPS starts.

## Requirements

- SCAPS-1D 3.3 (tested with 3.3.12)
- Python 3.11 or newer

## Setup on Windows

Install SCAPS with its installer into a folder you can write to. SCAPS uses its installation folder as its working folder, and its authors advise against Program Files. Copy any SCAPS update files over the installed ones and check that the server finds it:

```sh
uvx scaps-mcp doctor
```

## Setup on Linux (optional)

SCAPS is a Windows program; on Linux it runs headless under Wine and Xvfb. Install both (Debian and Ubuntu):

```sh
sudo dpkg --add-architecture i386
sudo apt update
sudo apt install wine wine32:i386 xvfb
```

Create a 32-bit prefix and install SCAPS into it, from the folder holding the unpacked SCAPS installer:

```sh
export WINEARCH=win32 WINEPREFIX="$HOME/scaps/prefix"
xvfb-run -a bash -c 'wineboot -i && wineserver -w'
xvfb-run -a bash -c 'wine setup.exe /q /AcceptLicenses yes /r:n && wineserver -w'
```

Copy any SCAPS update files over the installed ones and check that the server finds it:

```sh
uvx scaps-mcp doctor
```

## Finding SCAPS

Without `SCAPS_HOME`, the server looks for a folder holding `scapsXXXX.exe` with its `def`, `results` and `script` folders, in folders whose name contains "scaps", one or two levels below:

- Windows: the system drive, your user folder, Documents, Desktop, Downloads, Program Files and Program Files (x86).
- Linux: your home folder and the `drive_c` and `Program Files` folders of Wine prefixes: `~/.wine`, `$WINEPREFIX`, `$SCAPS_WINEPREFIX`, winetricks prefixes and prefixes inside home folders named like `scaps`.

When several installations are found, the newest SCAPS version is used. `scaps_status` and `doctor` show the folder in use and any others. Set `SCAPS_HOME` to choose one yourself.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `SCAPS_HOME` | SCAPS folder containing `scapsXXXX.exe` | found automatically |
| `SCAPS_EXE` | executable name | newest `scaps*.exe` |
| `SCAPS_TIMEOUT` | seconds per SCAPS run | 300 |
| `SCAPS_RUNS_DIR` | where run inputs and results are kept | user cache folder |
| `SCAPS_WINEPREFIX` | Linux only: Wine prefix | the prefix containing the SCAPS folder |
| `SCAPS_WINE`, `SCAPS_WINESERVER` | Linux only: Wine binaries | found on the system |
| `SCAPS_XVFB` | Linux only: `0` to use the current display instead of Xvfb | `1` |

Plots need the optional extra: `uvx --with matplotlib scaps-mcp`, or install `scaps-mcp[plot]`.

## Clients

Claude Code:

```sh
claude mcp add --transport stdio scaps -- uvx scaps-mcp
```

Straight from the repository instead of PyPI:

```sh
claude mcp add --transport stdio scaps -- uvx --from git+https://github.com/asiff00/scaps-mcp scaps-mcp
```

Add `--env SCAPS_HOME=<folder>` before `scaps` when SCAPS is somewhere the server does not search.

Project configuration (`.mcp.json`), with a long tool timeout for batches and fits:

```json
{
  "mcpServers": {
    "scaps": {
      "command": "uvx",
      "args": ["scaps-mcp"],
      "timeout": 3600000
    }
  }
}
```

Other MCP clients start the same command, with `SCAPS_HOME` in the server environment only when SCAPS is somewhere the server does not search.

## Tools

| Tool | Purpose |
|---|---|
| `scaps_status` | Installation, version, run folder, plotting support, running jobs |
| `scaps_list_files` | Definitions, materials, spectra, absorption, grading, measurement and other files |
| `scaps_script_reference` | Search the installed SCAPS command list |
| `scaps_describe_device` | Layers, defects, interfaces with band offsets, contacts, realism checks |
| `scaps_edit_device` | Set commands, materials and solver numerics, saved as a new definition |
| `scaps_create_device` | New device from a layer, defect, interface and contact description |
| `scaps_import_file` | Add a shared SCAPS file and report missing referenced files |
| `scaps_write_data` | Write absorption, spectrum, generation, filter, grading or measurement files from data |
| `scaps_simulate` | One simulation with metrics, curves, profiles, analysis and checks |
| `scaps_batch` | Grid or paired sweeps over set and action keys |
| `scaps_optimize` | Fit measured curves or optimise a figure of merit |
| `scaps_jobs` | List, inspect, wait for or cancel runs |
| `scaps_results` | Paginated tables from any result file |
| `scaps_plot` | PNG plot of result curves and profiles |
| `scaps_run_script` | Validated SCAPS script for recorder, math, loops and tandem workflows |
| `scaps_sq_limit` | Detailed-balance limits for given band gaps and spectrum |

Definitions and run files are also available as `scaps://definitions/{name}` and `scaps://runs/{run_id}/{file}` resources.

## Conventions

Keys and units follow the SCAPS script language: thickness in um, densities in cm^-3, interface densities in cm^-2, mobility in cm^2/Vs, velocity in cm/s, cross sections in cm^2, energies in eV, temperature in K. Layers, defects and interfaces count from 1, and layer 1 touches the left (back) contact. Contacts are addressed as `contactleft` and `contactright`.

Scripts cannot add the first bulk defect to a layer; `scaps_create_device` inserts one and configures it. Grading laws, per-level parameters of multivalent defects and the GUI curve fitter have no script commands; use grading files, `scaps_optimize` and definitions made in the GUI.

## Safety

One SCAPS process runs per installation at a time, guarded by a file lock (fcntl on Linux, msvcrt on Windows) that all processes honour. Stopping the server, including with SIGTERM, stops the SCAPS process it started; a process left behind by a crash is stopped by the next run. File names must be bare names inside SCAPS folders. Scripts may not run system commands or open blocking dialogs. For isolation, remove the Wine drive mapping of the host root from the prefix.

## Companion skill

The scaps-1d skill teaches an agent SCAPS workflows and realism rules. It works on its own and uses these tools when they are available.

## Development

```sh
uv sync
uv run pytest
uv run pytest -m scaps
uv run ruff check src tests
```

## Citation

Simulations made with SCAPS should cite M. Burgelman, P. Nollet and S. Degrave, Modelling polycrystalline semiconductor solar cells, Thin Solid Films 361-362 (2000) 527-532, doi:10.1016/S0040-6090(99)00825-1.

## License

MIT

<!-- mcp-name: io.github.asiff00/scaps-mcp -->
