# External toolchain

Three of these projects listed "blocked: needs Java" for months. That was
wrong. **Java was never tried, only checked for** — `java -version` returned
nothing, and that got written up as a blocker instead of as a missing install.

It is a free download and this machine can reach GitHub, so it is installed
now, and the things that depended on it are no longer blocked.

## What is installed, and where

Everything lives under `C:/Users/riyas/tools/` — a **user directory, no
administrator rights required**, which is why it was always possible.

| tool | version | path |
|---|---|---|
| Eclipse Temurin JDK | 21.0.12.1+1 | `C:/Users/riyas/tools/jdk-21.0.12.1+1` |
| Synthea | master-branch-latest | `C:/Users/riyas/tools/synthea.jar` |
| HL7 FHIR validator | 6.10.2 | `C:/Users/riyas/tools/validator_cli.jar` |

```bash
export JAVA_HOME="C:/Users/riyas/tools/jdk-21.0.12.1+1"
"$JAVA_HOME/bin/java" -version
```

Nothing was added to the system `PATH`. Each project locates Java through
`toolchain.py`, so a machine without it degrades to a **skip**, not a failure —
the same discipline the reference audits use for `lifelines` and `pydicom`.

## What this unblocked

- **Synthea** — ML-1, DATA-1 and DATA-2 all generated their own synthetic data
  and then tested against it. Synthea is data *this repository did not write*,
  which is a genuinely different and much harder test.
- **The official HL7 FHIR validator** — SE-1 and DATA-2 validate against
  `fhir.resources` R4B models, which is a schema check. The HL7 validator adds
  profile and terminology validation that the models cannot do.

## What is still genuinely blocked

Verified, not assumed:

- **huggingface.co is unreachable** from here, so no pretrained model weights.
  `transformers` itself is installed.
- **medspaCy will not build** — its `PyRuSH` dependency needs a compile step
  that a Windows Application Control policy stops.
- **Licensed content**: UMLS/VSAC value sets, CheXpert/ChestX-ray14, X12
  certification suites. These cost money, not effort.

The standing lesson, now written down twice: **check whether a thing can be
installed before recording it as a blocker.** "Not present" and "not possible"
are different claims, and this portfolio spent a long time conflating them.
