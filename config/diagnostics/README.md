# CANScope diagnostics — fault rule files

This folder holds the **YAML files that define what counts as a fault** in
each automotive subsystem. End users edit these files; you do not need to
write any Python.

After editing, click **Reload Rules** in the Diagnostics window
(`Ctrl + Shift + A`) to pick up your changes.

---

## File layout

One YAML file per **domain** (e.g. motor control, chassis, ADAS):

```
config/diagnostics/
├── motor_control.yaml        ← shipped example
├── chassis.yaml              ← add your own
├── adas.yaml
└── ...
```

The diagnostics engine auto-discovers every `*.yaml` / `*.yml` file in
this directory.

---

## File structure

Every domain file has three top-level keys:

```yaml
domain: <Display name>          # required — shown in the domain selector
description: <short paragraph>  # optional

context_window_s: 2.0           # optional — seconds of data captured before/after
                                 # each fault for AI diagnosis context (default 2.0)

rules:                          # list of detection rules
  - condition: <expression>
    ...
```

---

## Rule syntax

Every rule is a **condition expression** matched against the loaded
measurement:

```
SIGNAL  OPERATOR  VALUE  [and|or  SIGNAL  OPERATOR  VALUE ...]
```

**Operators:**

| Operator | Meaning                  | Example                         |
|----------|--------------------------|---------------------------------|
| `>`      | strictly greater than    | `condition: MotorTemp > 130`    |
| `<`      | strictly less than       | `condition: BusVoltage < 180`   |
| `>=`     | greater than or equal to | `condition: Pressure >= 250`    |
| `<=`     | less than or equal to    | `condition: Speed <= 0`         |
| `=`      | equals                   | `condition: Status = 3`         |
| `!=`     | not equal to             | `condition: FaultFlag != 0`     |

**Boolean connectors** — `and` / `or` (evaluated left-to-right):

```yaml
- condition: MotorTemp > 120 and MotorTemp < 135
- condition: Status = 3 or Status = 5
- condition: EDS_Err = 1 and EDS_opsts = 2
```

**Signal name matching** — names in the condition are matched
**case-insensitively** against the full signal store keys
(`CH<n>::<MsgName>::<SigName>`). A partial match is enough:
`MotorTemp` will match `CH1::MotorDrive::MotorTemp_degC`. If no
signal in the loaded measurement matches, the rule is silently skipped.

---

## Per-rule fields

Only `condition` is required. Everything else is optional.

```yaml
- condition: EDS_Err > 0          # required — the detection expression

  id: motor_fault_active          # unique identifier; auto-generated if omitted
  title: Motor fault flag active  # display name; defaults to the condition string
  severity: critical              # see Severity levels below (default: medium)
  enabled: false                  # set to false to disable without deleting (default: true)

  description: >                  # free-form text appended to the finding
    EDS fault flag was asserted. Check the EDS error log.

  suggested_action: >             # hint fed to the LLM
    Read out the EDS fault memory and compare with the inverter manual.

  plot_signals:                   # signals to auto-plot when this fault fires
    - EDS_Err                     # matched case-insensitively, same as condition
    - EDS_sts
    - EngSpeed
```

### `plot_signals`

When a rule fires the Diagnostics window can automatically add related
signals to the main plot and zoom to the fault time window.  List any
signal names you want visible alongside the fault trigger — they are
resolved case-insensitively against the measurement at run time. Signals
not present in the file are silently ignored. If `plot_signals` is
omitted, only the fault signal itself is plotted.

---

## Severity levels

```
info       informational only     blue
low        advisory               green
medium     warning                yellow   (aliases: warn, warning)
high       error                  orange   (aliases: error)
critical   immediate action       red      (aliases: fatal)
```

Pick the severity that reflects the operational impact, not how
"interesting" the data is.

---

## Minimal rule examples

```yaml
domain: Motor Control
description: Fault rules for motor controllers and inverters.

context_window_s: 1.0

rules:

  # Fault flag: fire whenever EDS_Err is non-zero
  - condition: EDS_Err > 0
    severity: critical
    plot_signals:
      - EDS_Err
      - EDS_opsts

  # Range violation: temperature outside operating band
  - condition: MotorTemp > 130
    title: Motor over-temperature
    severity: high

  # Compound: flag only when both conditions are true simultaneously
  - condition: BusVoltage < 200 and MotorTemp > 120
    title: Low voltage + high temperature
    severity: critical
    description: Possible cooling failure under reduced supply.

  # Status enum: specific error state
  - condition: InvStatus = 5 or InvStatus = 6
    title: Inverter error state
    severity: high

  # Disabled rule — kept for reference
  - condition: MotorTemp > 110
    title: Motor temperature advisory
    severity: low
    enabled: false
```

---

## Adding a new rule (worked example)

To detect an over-current condition on the motor U-phase:

1. Add the rule to the relevant domain YAML:

```yaml
rules:
  - condition: PhaseU_Current > 600 or PhaseU_Current < -600
    title: Phase U over-current
    severity: high
    description: >
      Phase U current exceeded peak rated value (±600 A). Possible causes:
      short circuit, current sensor offset error, or PWM saturation.
    suggested_action: >
      Inspect gate driver faults and current sensor calibration.
    plot_signals:
      - PhaseU_Current
      - PhaseV_Current
      - PhaseW_Current
```

2. Save the file. Click **Reload Rules** in the Diagnostics window.
3. Run a measurement — the new rule appears in the findings list.

---

## Adding a new domain

Create a new YAML file in this folder, e.g. `chassis.yaml`:

```yaml
domain: Chassis
description: Brake, steering and ESP fault rules.

context_window_s: 2.0

rules:
  - condition: BrakePressureFront > 250
    title: Front brake pressure over limit
    severity: high
    plot_signals:
      - BrakePressureFront

  - condition: ABS_Fault != 0
    title: ABS fault active
    severity: critical
```

The file appears in the domain selector automatically — no other changes needed.

---

## Disabling a rule without deleting it

Set `enabled: false` on any rule:

```yaml
- condition: MotorTemp > 110
  enabled: false
```

---

## Validation errors

When a YAML file is invalid the Diagnostics window shows the file name
and a precise reason (missing `condition`, unparseable expression,
duplicate `id`, bad severity value...). Fix the file and click
**Reload Rules**.

Common mistakes:

| Error | Cause |
|-------|-------|
| `each rule needs a 'condition' expression` | `condition` key missing or empty |
| `Cannot parse: '...'` | Expression is not in `SIGNAL OP VALUE` format |
| `severity must be one of ...` | Unknown severity string |
| `duplicate rule id '...'` | Two rules share the same `id` |

---

## What goes to the AI

When you click **Run Analysis** the engine:

1. Runs all enabled rules locally on the loaded measurement.
2. Builds a small **evidence packet** for each finding: a statistical
   summary plus a downsampled snippet (≤ 100 points) around the event,
   covering `context_window_s` seconds before and after the fault.
3. Sends only the findings + evidence to the LLM (≤ 50 KB total).

**The full measurement never leaves your machine.** A 500 MB log produces
the same ~50 KB AI payload as a 5 MB log.
