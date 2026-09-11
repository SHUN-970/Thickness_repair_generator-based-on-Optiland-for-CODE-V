# CODE V / OPTILAND THICKNESS REPAIR TOOL

## Purpose

`thickness_repair.py` creates thickness-repaired lens candidates from a CODE V `.seq` file. It preserves the verified legacy power-preserving mechanism, rebuilds the system in Optiland, checks the constraints, performs real-ray validation, and exports new CODE V files and lens-layout images.

The input `.seq` file is read only and is not overwritten. EFL is measured automatically and is not entered by the user.

Each normal run uses a fresh system-generated random seed. Repeating the same constraints and lens selection may therefore produce different valid candidate structures.

## Recommended Scope

This tool is intended mainly for:

- Initial optical structures rather than fully finished production designs.
- Sequential systems using spherical lens surfaces.
- Designs with sufficient air-gap and total-length margin.
- Moderate center-thickness corrections where the initial and target thicknesses are reasonably close.

## Running in VS Code

1. Open the folder containing `thickness_repair.py` in VS Code.
2. Open `thickness_repair.py`.
3. Select a Python interpreter in which Optiland and the required dependencies are installed.
4. Click **Run Python File in Terminal**.
5. At the first prompt, enter the full path of the CODE V `.seq` file to repair.  
   This input is required; the program does not contain a default file path.
6. Enter the four requested constraints:
   - Minimum glass center thickness
   - Minimum glass edge thickness
   - Minimum center air gap
   - Maximum total thickness

   All four values are required. The program does not contain default constraint values.

7. Select the physical lenses to repair. Examples:

   ```text
   L3 L4 L5
   L3-L7
   ```

   Press **Enter** to repair every CT/ET-noncompliant lens. Enter `NONE` to cancel.

8. Enter a brand-new output-folder path, or press **Enter** to create an automatic timestamped folder. An existing output path will not be overwritten.

## Command-Line Example

Run the following from the script folder in a PowerShell terminal.

The numbers below are example user inputs only; they are not program defaults.

```powershell
python .\thickness_repair.py `
  "C:\path\to\input.seq" `
  --ct-min 1.0 --et-min 0.2 `
  --center-gap-min 0.1 --ttl-max 13.5 `
  --lenses L3-L7 `
  --output-dir "C:\path\to\new_output" `
  --non-interactive
```

The command may also be entered on one line.

## Output Files

The new output folder contains:

- Ray-valid candidate `.seq` files
- A physical-lens layout for every candidate
- Input and thickness-diagnosis layouts
- `diagnosis.txt`
- `candidate_summary.csv`
- `generation_report.txt`

## Limitations and Possible Failure Cases

The search may fail to produce a valid candidate when the requested constraints are too severe for the initial structure. Typical examples include:

- A maximum total thickness that is too short.
- Lenses or components that are already tightly crowded.
- Insufficient air-gap or edge-clearance margin.
- A very large difference between the initial lens thickness and the requested target thickness.
- A combination of thickness, spacing, EFL, and ray-trace requirements that has no feasible solution within the preserved search method.
- Unsupported or strongly non-spherical geometry.

A failed run does not mean that the optical design is impossible. It means that no valid candidate was found by this repair method under the selected constraints.

Always inspect the exported candidates in CODE V and perform the required image-quality, tolerance, and manufacturability checks before using a result in a final design.
