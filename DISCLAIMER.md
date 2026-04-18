# Disclaimer

## Offline Analysis Only

CAN Scope is designed exclusively for **offline post-processing** of measurement
files. It must **not** be used in any safety-critical, real-time, or
production-control context.

## No Warranty

This software is provided "as is", without warranty of any kind, express or
implied. The authors and contributors accept no liability for any direct,
indirect, incidental, or consequential damages arising from its use.

## Third-Party Dependencies

CAN Scope depends on several open-source libraries
(python-can, cantools, PySide6, pyqtgraph, numpy, asammdf). Each is governed
by its own licence. Please review those licences before redistributing.

## No Affiliation

This project is not affiliated with, endorsed by, or connected to:

- **Vector Informatik GmbH** — makers of CANalyzer, CANdb++, and the BLF /
  ASC file formats
- **ASAM e.V.** — authors of the MDF specification
- **Qt Group** — makers of the Qt framework

Product names, trademarks, and logos mentioned are the property of their
respective owners and are used here solely for the purpose of identifying
compatible file formats.

## Data Privacy

CAN Scope processes measurement files entirely **locally on your machine**.
No data is transmitted to any server. No telemetry is collected.

## Use at Your Own Risk

Always verify results against an authoritative tool such as CANalyzer or INCA
before drawing engineering conclusions.
