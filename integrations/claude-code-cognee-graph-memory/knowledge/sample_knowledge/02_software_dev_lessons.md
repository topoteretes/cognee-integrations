# Software development lessons

## Principles of test design

Unit-test expected values must be derived from the IF (interface) specification.
Reading the implementation code to determine expected values is forbidden. A
test driven by the implementation cannot detect bugs.

## V-model: design and test pairing

Requirements <-> system test. High-level design <-> external integration
test. Detailed design <-> internal integration test. Implementation <-> unit
test. Expected values must always come from the matching design document.

## Aligning test and production environments

If ports, protocols, or configuration differ between test and production,
tests can pass while production fails. HTTP/HTTPS mixing and port mismatches
in particular cause false PASSED results in integration tests.

## Separating design from procedure

Design documents describe what to build and how it is structured. Procedure
documents describe how to set it up. Mixing the two in one document buries the
design intent inside operational instructions.

## Order of consistency checks

When you find a problem during a check, do not fix it on the spot. Finish the
full check, transcribe the issues into the unresolved list, then address them
one by one in list order. Fix-on-discovery breeds oversights and chaos.

## Where to store documents

Each artifact's location is fixed by its phase and role. In-phase check records
go to the check folder. Cross-phase review results go to the review folder.
Re-investigations after issues go to the quality cross-check folder. The wrong
location makes the artifact unfindable later.
