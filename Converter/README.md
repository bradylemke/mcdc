# MC/DC to ParaView Converter

*By Brady Lemke, Oregon State University*

Tools for viewing MC/DC problems in ParaView.
The converter turns an MC/DC input file into a geometry file and an MC/DC output (`.h5`) file into tally files, and both open directly in ParaView.

## Features

- Geometry extracted straight from the input `.py` file, without running MC/DC
- Volume tallies (flux) written as `.vtr` and surface tallies (current) written as `.vtp`
- One command converts both the input and output file
- Works on any MC/DC input or output file, with no per-problem setup

## Requirements

- Python 3 with `numpy`, `h5py`, and `vtk`
- ParaView 6.1.1
- The converter scripts, kept together in one folder
- An MC/DC input file (`.py`) and/or output file (`.h5`) to convert

## Installation

Install the Python packages with pip:

```
pip install numpy vtk h5py
```

Download ParaView 6.1.1 for your system from [paraview.org/download](https://www.paraview.org/download/) and install it.
Skip either step if it is already installed.

## Converting Files

Open a terminal and move into the folder that holds the converter scripts:

```
cd path/to/converter_scripts
```

Run `mcdc_convert.py` with your input and output files:

```
python3 mcdc_convert.py --input your_file.py --output your_file.h5
```

Either one can be run on its own:

```
python3 mcdc_convert.py --input your_file.py
python3 mcdc_convert.py --output your_file.h5
```

The script prints its progress and writes the new files next to the input or output file it was given.
It creates the following files:

| File                            | Contents                                                      |
| ------------------------------- | ------------------------------------------------------------- |
| `your_file_geometry.vtp`        | The full geometry, plus one `_geometry.vtp` file for each shape |
| `your_file_<tally>_t000.vtr`    | Volume tally data, one file per time step                     |
| `.vtp` from the output file     | Surface-current tallies                                       |

### Running the tally scripts on their own

`--input` and `--output` are the only flags `mcdc_convert.py` takes.
To look at just the tally data, run the tally scripts directly:

```
python3 h5_to_vtr.py your_file.h5
python3 h5_to_vtp.py your_file.h5
```

To convert a single tally, put its name after the file:

```
python3 mcdc_auto_convert.py your_file.h5 tracklength_tally_0
```

For surface tallies where the box cannot be worked out automatically, `mcdc_auto_convert.py` and `h5_to_vtp.py` also take `--bounds x0 x1 y0 y1 z0 z1`.

### Supported Shapes

The geometry extractor reads these surfaces from the input file:

- Planes: `PlaneX`, `PlaneY`, `PlaneZ`
- Spheres: `Sphere`
- Cylinders: `CylinderX`, `CylinderY`, `CylinderZ`

Regions built from these with `&` (and) and `|` (or) are supported, including boxes, combined boxes, and shapes with spheres and cylinders.
Write surface arguments as keywords (e.g. `radius=2.0`). Their order does not matter.

If your problem uses other shapes, from simple ones like cones to more complex geometry, email me at lemkeb@oregonstate.edu and I can add or adjust the extractor for it.
If the converter prints a warning about a cell, send me the input file and I can look at it.

## The Converter Files

Only `mcdc_convert.py` is run directly.
It calls the other four.

| File                    | Description                                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------- |
| `mcdc_convert.py`       | Reads `--input` and `--output` and calls whichever of the other files it needs.                   |
| `extract_geometry.py`   | Reads the geometry from the input file. Tries the box reader first, then the curve reader.        |
| `mcdc_auto_convert.py`  | Checks what kind of tallies the `.h5` file has and sends them to the right converter.             |
| `h5_to_vtr.py`          | Writes volume (flux) tallies as `.vtr`. This is the file to load first in ParaView.               |
| `h5_to_vtp.py`          | Writes surface-current tallies as `.vtp`.                                                         |

The box reader matches planes combined into a box shape, which is exact and fast.
If a cell is not a plain box, the curve reader handles spheres and cylinders using VTK boolean shapes.

## Opening Files in ParaView

1. Open ParaView.

2. Go to **File > Open**, select the `.vtr` file, and click **Apply**.

3. Change the coloring dropdown from Solid Color to the tally name (e.g. `flux`).

4. Change the representation from Outline to **Volume**.

5. Go to **File > Open** again, select the `_geometry.vtp` file, and click **Apply**.

6. Set this object's coloring back to Solid Color and lower its **Opacity** to about 0.2 to 0.3.

7. Optional: add a **Threshold** filter on the tally to hide the zero-flux cells.

## Reading Values in ParaView

To see the exact value of a cell, open a **SpreadSheet View** from the split buttons at the top right of the Render View and set **Attribute** to **Cell Data**.
Click a column header to sort, and raise **Precision** to show more digits.

To select cells in the 3D view, drag a box with one of these hotkeys:

| Key | Selects                   |
| --- | ------------------------- |
| `S` | Visible cells             |
| `D` | Visible points            |
| `F` | Cells through the volume  |
| `G` | Points through the volume |

Hold `Ctrl` (`Cmd` on Mac) to add to a selection and `Shift` to subtract.
Press `V` to open **Find Data**, which selects cells by value (e.g. `flux` `is max`).
Set the color map to log scale (**View > Color Map Editor**) so low-flux cells are visible.

## Troubleshooting

| Problem                                      | Fix                                                                                   |
| -------------------------------------------- | ------------------------------------------------------------------------------------- |
| The volume is blank or black                 | The coloring dropdown is probably still on Solid Color. Set it to the tally name.     |
| Everything is one color                      | Turn on log scale and set a custom range.                                             |
| Nothing changed after editing a setting      | Click **Apply**.                                                                      |
| The data disappeared after adding a filter   | Click **Reset Camera** and check the eye icon for the new item.                       |
| The geometry hides the flux                  | Lower its Opacity or turn it off.                                                     |
| Cells on the inside will not select          | Use `F` instead of `S`.                                                               |
| The geometry looks blocky                    | This is the resolution of the tally mesh, not a ParaView setting.                     |
| The converter cannot find a file             | Run the command from the folder with the scripts and pass the full path to the input or output file, e.g. `--input /path/to/your_file.py`. |

## Reporting Bugs

If you run into any trouble or find a bug, email me at lemkeb@oregonstate.edu.
