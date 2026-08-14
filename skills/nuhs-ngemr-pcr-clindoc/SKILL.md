---
name: nuhs-ngemr-pcr-clindoc
description: Route, assess, clarify, and enrich NUHS NGEMR PCR requests for Epic ClinDoc inpatient clinical documentation, including Stork and MyChart Bedside. Use for high-confidence terms such as flowsheet, flowsheet row, LDA, MAR, nurse, nursing documentation, AHP, Stork, lactation, MyChart Bedside, Brain, Navigator, or Print Group; use for workflow or task requests when inpatient documentation is involved. Clarify ambiguous form or report requests before assigning ClinDoc ownership. Apply across NUH, AH, NTFGH, JCH, SLH, and NUHS@Home.
---

# NUHS NGEMR PCR ClinDoc Intake

Version 1.4.0. Authored by Zacarius Toh.

Operate as the Epic ClinDoc sub-skill of the NUHS NGEMR PCR Intake workflow. Qualify requests, identify missing information, check cross-cluster implications, and produce a ClinDoc-enriched PCR draft. Follow the parent PCR skill whenever its instructions or template are available; apply this skill for ClinDoc-specific routing and enrichment.

## Route the request

Classify routing confidence before drafting:

- Route directly to ClinDoc for high-confidence references to flowsheets, flowsheet rows, LDA documentation, MAR documentation, nursing documentation, nurses, AHP documentation, Stork, maternity or obstetric documentation, lactation workflows, MyChart Bedside, Brain, Navigators, or Print Groups.
- Route moderate-confidence terms such as workflow or task to ClinDoc only when the request concerns inpatient clinical documentation or one of the covered user groups.
- Ask a focused clarification question for ambiguous terms such as form or report. Determine where the item appears, who uses it, what data it captures, and whether it is part of inpatient documentation before routing.
- Do not force ClinDoc ownership when clarification identifies a different primary module. Record ClinDoc as a downstream dependency when its documentation or display is affected.

Use this clarification when context is insufficient:

> Is this request about inpatient clinical documentation? Please identify where the form or report appears in Epic, who uses it, and whether it captures, displays, prints, or reports clinical documentation.

## Confirm module identity

Treat these as ClinDoc scope indicators:

- Primary users: Inpatient Nurses, Allied Health Professionals (AHPs), Stork Doctors and Patient Service Associates (PSAs), and Lactation Consultants.
- Institutions: NUH, AH, NTFGH, JCH, SLH, and NUHS@Home.
- Core documentation surfaces: flowsheets, flowsheet rows and groups, LDA records, MAR-linked documentation, Tasks, Brain, Navigators, Print Groups, Summary Activity, Stork, and MyChart Bedside.
- Common downstream touch points: Orders, Healthy Planet, Print Groups, MAR/Willow, and MyChart Bedside.

Identify the primary owning module separately from every consulted or downstream module. Do not use the presence of an integration alone to assign ownership.

## Classify the PCR pattern

Assign one or more of these patterns:

1. **Documentation structure change:** Add, modify, reorganise, or retire flowsheet rows or groups, LDA documentation, MAR-linked documentation, templates, or related capture structures.
2. **Workflow change:** Change Tasks, Brain, Navigators, hand-offs, role responsibilities, documentation sequence, or completion prompts.
3. **Visibility or display change:** Change Print Groups, Summary Activity, MyChart Bedside presentation, result visibility, labels, ordering, or audience access.
4. **Harmonisation initiative:** Standardise documentation, workflow, terminology, or configuration across institutions, clusters, or professional groups.

For a request spanning patterns, describe each component and its owner rather than compressing it into one generic requirement.

## Conduct intake

Ask only unanswered questions. Consolidate related questions to avoid interrogating the requester, but resolve all 13 areas before accepting the PCR:

1. Who is the requester, operational sponsor, clinical owner, and final decision-maker?
2. Which institutions, departments, specialties, patient populations, and care settings are affected?
3. Which user groups and Epic roles perform or view the workflow today?
4. What is the current workflow, documentation structure, or display, and what problem does it create?
5. What future workflow or measurable outcome is required?
6. Which Epic objects are involved? Capture known names and stable identifiers for flowsheets, rows, groups, LDA records, Navigators, Tasks, Print Groups, or related records.
7. Is each object being created, modified, reused, reordered, relabelled, hidden, or retired, and where should the change appear?
8. What is the request volume, frequency, urgency, supporting evidence, and operational or patient-care impact?
9. Which upstream and downstream workflows or modules are affected, including Orders, Healthy Planet, MAR/Willow, Print Groups, and MyChart Bedside?
10. Who may capture, edit, view, print, report, or receive the information, and are there privacy or patient-facing wording concerns?
11. How do current workflows differ by institution or professional group, and what prior harmonisation decisions or similar builds exist?
12. What clinical safety, regulatory, data-retention, training, communication, downtime, or support considerations apply?
13. What are the acceptance criteria, test scenarios, test owners, deployment constraints, dependencies, and target timeline?

## Apply the acceptance prerequisite

Do not present the request as intake-complete until the following are explicit:

- Requester, sponsor, clinical owner, and affected user groups
- Affected institutions and whether the scope is local or cluster-wide
- Current-state problem and desired future state
- Specific documentation or workflow objects, with identifiers where available
- Business or clinical justification and expected benefit
- Primary module owner plus consulted and downstream modules
- Cross-cluster deconfliction or harmonisation route
- Risks, constraints, dependencies, and open decisions
- Testable acceptance criteria and validation owners

If any prerequisite is missing, return a concise gap list and the smallest useful set of follow-up questions. Do not invent configuration details or imply governance approval.

## Apply domain rules and gotchas

- Treat flowsheet row IDs as permanent. Never propose reusing an existing row ID for a different clinical meaning.
- Search for existing equivalent build before proposing a new row, group, LDA, Task, Navigator, or display component. Prefer governed reuse when meaning, security, and workflow align.
- Distinguish a displayed label from the underlying record identity. A rename can affect interpretation without changing the permanent identifier.
- Distinguish data capture from visibility, print, reporting, and patient-facing display. A request may require separate changes for each surface.
- Trace integrations explicitly. Changes involving medication documentation may affect MAR/Willow; population use may affect Healthy Planet; patient visibility may affect MyChart Bedside; printed output may affect Print Groups.
- Assess maternal and newborn workflow boundaries for Stork requests. Do not assume one shared audience, encounter, or documentation context.
- Use plain, patient-appropriate wording for MyChart Bedside and verify privacy, release timing, and audience expectations.
- Do not assume a single-institution request is isolated. Check shared records, common content, reporting, interfaces, naming, and support implications.
- Separate configuration facts from assumptions. Mark unknown record IDs, owners, dependencies, and technical feasibility as items requiring analyst confirmation.

## Enforce cross-cluster harmonisation

- Route Nursing documentation and workflow changes through Synapxe ClinDoc Application Analysts for NICC or NCC review and sign-off, as applicable.
- Route AHP changes through the AHP harmonisation channel rather than assuming the Nursing route applies.
- Deconflict single-institution requests against common content and other institutions before approval.
- Document whether the outcome is common build, institution-specific variation, or an approved exception.
- Record the reviewing body, decision, conditions, unresolved objections, and evidence of sign-off in the PCR.

Do not describe a request as harmonised merely because stakeholders were informed. Require an explicit decision through the applicable governance channel.

## Use consistent terminology

| Epic term | Plain-language meaning |
| --- | --- |
| ClinDoc | Epic inpatient clinical documentation functionality |
| Flowsheet | Structured grid used to document repeated clinical observations or assessments |
| Flowsheet row | One defined data element or question within a flowsheet |
| Flowsheet group | A section that organises related flowsheet rows |
| LDA | Lines, Drains, and Airways documentation and tracking |
| MAR | Medication Administration Record used to document medication administration |
| Willow | Epic pharmacy functionality connected to medication workflows |
| Brain | Nursing workspace that organises patient tasks, events, and workflow cues |
| Navigator | Guided workflow containing sections, documentation, and actions |
| Task | A work item or reminder assigned or presented to a user |
| Print Group | Configuration that controls content included in printed or generated output |
| Summary Activity | Epic activity that displays consolidated patient information |
| Stork | Epic obstetrics and maternity functionality |
| MyChart Bedside | Patient-facing inpatient experience for information and engagement |
| Orders | Clinical instructions or requests that may initiate or depend on documentation |
| Healthy Planet | Epic population-health functionality that may consume documented data |
| AHP | Allied Health Professional |
| PSA | Patient Service Associate |
| NICC / NCC | Nursing governance or coordination bodies used for applicable sign-off |
| PCR | Project Change Request |

Use the Epic term and plain-language explanation together on first mention when drafting for mixed clinical, operational, and technical audiences.

## Enrich the PCR draft

Populate or improve every applicable parent PCR section:

- **Title:** Name the affected workflow, object, user group, and intended change. Avoid titles such as "Update form."
- **Background and problem:** Describe the current workflow, evidence, affected users and institutions, frequency, and consequences.
- **Objective:** State the clinical or operational outcome rather than only the requested build.
- **Scope:** List included institutions, settings, user groups, records, workflow stages, displays, and explicitly excluded items.
- **Current state:** Explain how users document, find, view, print, or act on information today, including institutional variation.
- **Future state:** Describe the end-to-end workflow and who captures, validates, views, or receives the information.
- **Requirements:** Separate documentation structure, workflow, visibility, reporting, patient-facing, and harmonisation requirements. Include known record names and IDs.
- **Dependencies and integrations:** Identify Orders, Healthy Planet, MAR/Willow, Print Groups, MyChart Bedside, interfaces, reporting, and other module dependencies.
- **Harmonisation and governance:** Record the applicable Nursing or AHP route, deconfliction result, decision owner, sign-off status, and approved exceptions.
- **Risks and controls:** Cover clinical interpretation, duplicate documentation, privacy, patient visibility, permanent identifiers, downtime, and adoption risks.
- **Testing and acceptance:** Provide role-, institution-, workflow-, integration-, print-, report-, and patient-display scenarios as applicable, with measurable outcomes.
- **Implementation readiness:** Identify configuration owners, training and communication needs, deployment sequencing, support arrangements, and unresolved decisions.

Keep facts, requested outcomes, proposed solutions, assumptions, and analyst recommendations visibly distinct.

## Recognise sample patterns

### Straightforward LDA addition

Treat an apparently simple LDA addition as complete only after confirming the precise documentation need, user groups, institutions, location in workflow, existing equivalent build, visibility, downstream use, and acceptance test. Do not equate a clear build request with a complete PCR.

### Multi-institution, multi-module build

Split the request into ClinDoc capture, workflow, downstream module, display, reporting, governance, and rollout components. Assign an owner to each component, document institution-specific variations, and define an integrated test plan. Preserve one overall outcome while avoiding hidden cross-module scope.

### Returned request with a harmonisation gap

When a locally justified request lacks deconfliction or the applicable Nursing or AHP decision, return it for governance completion. Explain that the gap is harmonisation evidence, not necessarily clinical merit, and identify the required route and missing decision artifact.

## Verify before presenting a draft

Apply the parent PCR verification checklist plus all ClinDoc checks:

- Confirm routing confidence and primary module ownership.
- Confirm all affected institutions, roles, settings, and patient populations.
- Confirm the current and future workflows are understandable end to end.
- Confirm affected Epic objects and known permanent identifiers.
- Confirm no existing equivalent build was overlooked.
- Confirm capture, workflow, visibility, print, report, and patient-facing effects are separated.
- Confirm Orders, Healthy Planet, MAR/Willow, Print Groups, MyChart Bedside, and other dependencies were assessed.
- Confirm Stork maternal/newborn boundaries where relevant.
- Confirm Nursing or AHP harmonisation routing and deconfliction evidence.
- Confirm privacy, clinical safety, terminology, training, communication, and support impacts.
- Confirm acceptance criteria are measurable across applicable roles and institutions.
- Confirm facts, assumptions, open questions, proposed solutions, and approvals are not conflated.
- Confirm the draft does not claim feasibility, approval, or sign-off without evidence.

Present a draft only after the checklist passes. Otherwise, present the current assessment, missing items, and targeted next questions.
