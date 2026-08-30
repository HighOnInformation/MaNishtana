# Close the claim-verification loop, and make the verdict machine-readable

**Status:** spike run, survived with one reshape. **Cost:** S.

## Problem

`0c0fcae` states what this tool is for: it is *"the what actually changed stage of
a patch-verification service — the service that asks whether a reported
vulnerability was genuinely fixed in a GitHub repository, rather than merely
touched."* Answering that question means holding two things side by side: what the
commit messages **claim**, and what the diff **does**.

`1cc3571` built almost all of it and stopped one step short, in two places.

**1. The claim never reaches the pass that could adjudicate it.**

On `main`, `llm.py:93-95` folds the commit messages into `context`:

```python
context = prompts["context_block"].format(brief=brief) if brief else ""
if intent:
    context = prompts["intent_block"].format(intent=intent) + context
```

and `context` is passed only to `_analyze_file`. The summary call, `llm.py:114-118`,
takes two things and neither is the claim:

```python
prompts["summary_user"].format(manifest=manifest, analyses=body),
```

So every per-file call is asked to check the claim against one file, and the one
pass that sees the change whole — the reconciliation summary, the pass `8b4c72b`
added precisely because *"a file read in isolation cannot be understood"* — is
never told what the change claims to be. That is backwards for this claim shape.
"Fixes CVE-1234" is not a statement about `parser.c`; it is a statement about the
change. A per-file pass can only ever answer "does this file look consistent with
that sentence", which is the wrong question asked N times.

**2. There is no machine-readable verdict.**

`build_json` (`output.py:195-234` on `main`) emits `commits` and
`inputs.intent_shown` (`output.py:228-231`) — the claim, and whether the model was
allowed to see it — and nothing that says whether the diff supports it. The
summary object is `{"text": ..., "risk": ...}` (`output.py:226`). A consuming
service that wants "was this actually fixed?" has to parse English prose out of
`summary.text`. The README already sells `counts.real == 0 && counts.noise > 0` as
the machine-readable form of "they changed the file but not the code"; there is no
equivalent for "they changed the code but not in the way they said".

The default that withholds the messages is **correct and is preserved**. `1cc3571`
argues it well: *"A model shown the author's description of a change reproduces it,
which destroys the comparison worth making."* Nothing here changes what happens
without `--intent-context`.

## Approach

Five small edits, no new module, no new dependency, no change to the dependency
direction (`settings` still imports nothing from the package).

1. **`settings.py:118-125` — a new prompt, `claim_block`**, required placeholder
   `{intent}`. It carries the author's account *and* the request for the verdict
   line, in one reviewable text.
2. **`settings.py:91` and `settings.py:95`** — `claim_block` is registered in
   `_PLACEHOLDERS`, and `summary_user` gains `intent` as an **allowed but not
   required** placeholder. A config that overrode `summary_user` before this
   existed has no `{intent}` slot and must keep loading — the backward-compatibility
   rule `8b4c72b` set when it externalized the prompts.
3. **`settings.py:134` — `risk.claim_label: "Claim"`**, beside `file_label` and
   `summary_label`, because extraction looks for the literal label the prompt asks
   for. `8b4c72b` made that coupling an invariant and put the labels in one block
   so a translator moves them together; this is the same input, so it lives in the
   same place. (The block is named `risk` and now holds something that is not a
   risk. Renaming it would break every existing config for a cosmetic gain, so it
   stays.)
4. **`llm.py:96-102, 121-127`** — when `intent` is supplied, render `claim_block`
   and pass it to the summary call as `{intent}`. When it is not, pass `""`.
5. **`output.py:710-740` — `_extract_claim`**, and `summary.claim` in `build_json`
   (`output.py:241-248`).

`cli.py` is untouched: `cli.py:122` already computes `intent` from
`--intent-context` and `cli.py:146` already sets `intent_shown`.

### One reshape: the request for the verdict travels with the claim, not in `summary_system`

The obvious move is to extend the default `summary_system` to always ask for the
`**Claim**` line. I tried that shape on paper and rejected it. `summary_system` has
no placeholders and cannot be conditional, so every run — including the default,
withheld-intent run, which is the *common* case — would end with the model asked to
declare a claim supported or not when it was shown no claim. The model then either
invents a verdict or writes prose about not having one, and `_extract_claim` may
scrape a value out of it. A fabricated `summary.claim` on the independent path is
worse than no field at all: it is precisely the pathology `1cc3571` engineered
against, reintroduced through the back door.

Putting the request inside `claim_block` makes it structurally impossible: no
claim, no request, no verdict. The alternative — a conditional inside
`summary_system` ("omit this section if no claim was supplied") — spends tokens on
every run to ask the model to police a condition the caller already knows.

### One trap, and it is not the same trap `_extract_risk` had

`_extract_risk` (`output.py:682-707`) solves "review prose names a level without
declaring one": *"reduces the risk of a high reading"* must not escalate a Low
verdict. Its fix is that the label must introduce a field, and a line echoing all
three levels is rejected. `_extract_claim` needs both of those, and one more.

Risk levels are three distinct words. Claim verdicts are three phrases that all end
in the same word:

```
Supported / Partly supported / Not supported
```

`_extract_risk`'s rule — *first level named anywhere after the label wins* — applied
here reads `**Claim** — Not supported` and can return `supported`. It **inverts the
answer**, and it inverts it in the direction that says a vulnerability was fixed
when the model said it was not. So `_CLAIM_RE` (`output.py:41`) is **anchored**: the
verdict must be the first words after the label, exactly as `claim_block` demands.
Anything else is `None`, which a consumer reads as "the model did not answer in the
required form" — the safe direction for a field that gates a patch. The template-echo
guard still needs an unanchored scan (`_CLAIM_ANY_RE`, `output.py:45`), because
anchoring alone would read the echoed template `Supported / Partly supported / Not
supported` as a confident `supported`.

### `schema_version`

**This proposal adds one field: `summary.claim`, a string or `null`.** It does not
bump `schema_version`, which stays at 2 with a `# Pending:` note at
`output.py:225-230`. Three proposals each planned to own version 3; the versioning
rule is being settled separately and decides what this becomes. `CLAUDE.md` requires
a bump for a **breaking** change to the JSON, and an added key is additive — a
consumer reading `schema_version == 2` today gets everything it got before — so not
bumping is defensible on the repo's own rule. It is still a loose end: an unbumped
version means a consumer cannot detect the field's presence except by looking for
it. The version rule must resolve that, and this proposal does not pre-empt it.

## What the spike showed

The spike is `ClaimVerdict` in `tests/test_manishtana.py:1058-1100` — 43 lines, five
tests, on the existing `FakeChat` seam, no network. `python -m unittest discover -s
tests` is **162 tests green** (157 on `main` plus these five). Everything below was
observed by running, on Windows, Python 3.12.

**The claim reaches the summary.** With `intent="Fixes CVE-1234, a heap overflow in
the parser."`, the last chat call — asserted to be the one labelled `executive
summary` — carries:

```
The author describes the whole change as follows. Treat it as a claim to
adjudicate against the code, not as established fact.

Fixes CVE-1234, a heap overflow in the parser.

End with a separate line in this exact format:

**Claim** — Supported, Partly supported or Not supported, followed by one
sentence naming the evidence in the diff. ...

Change inventory
1 real, 0 noise only, 0 skipped.
...
```

**Without it, nothing.** The same call with no `intent` contains neither the claim
text nor the string `**Claim**`. The default path is unchanged.

**The verdict round-trips.** A canned summary containing
`**Claim** — Not supported, only comments.` produces
`payload["summary"]["claim"] == "not supported"` alongside
`payload["summary"]["risk"] == "low"` from the same text — the two extractors do not
interfere. A summary with no claim line yields `None`.

**The inversion trap is real and is caught.** `**Claim** — Not supported, no test.`
→ `"not supported"`. `**Claim**: Partly supported, one of two.` → `"partly
supported"`. The echoed template → `None`. Unlabelled prose *"The fix is not
supported by any test."* → `None`. And `**Claim** — Partially supported.` → `None`,
not `"supported"`: an unrecognised phrasing fails closed rather than resolving to
the most favourable verdict. That last one is the case I would have shipped a bug
in without writing it down.

**Backward compatibility holds.** A config overriding `summary_user` as
`"{manifest} then {analyses}"` — no `{intent}` slot — loads without error, runs with
`--intent-context`, and simply produces a summary with no claim in it. Unknown
placeholders are still rejected, and `claim_block` without `{intent}` is still a
startup error.

**The full CLI path works.** `python -m manishtana --git . --commit 0424fa6
--intent-context -j out.json` against this repository, with no endpoint listening:
the chat call fails and warns as designed, the report renders *"this text **was**
supplied to the model"* in place of the independence note, and the JSON carries
`inputs.intent_shown: true` with `summary.claim: null`. That is the correct
degraded shape — a consumer sees "the claim was shown, and no verdict came back".

## The independence question, and how a consumer should treat an `intent_shown` verdict

This is the part the spike **cannot** settle, and it is the part that decides
whether the feature is worth anything.

`1cc3571` is right that a model shown the author's description tends to reproduce
it. A verdict is worth something only if the model will actually say *Not supported*
when the diff does not support the claim. Sycophancy pushes exactly the other way:
the most likely failure of this feature is a `summary.claim: "supported"` that means
nothing but "the model read the commit message". A field that is 95% "supported"
regardless of the diff is worse than no field, because it looks like a signal.

**What evidence would settle it.** A labelled corpus and a two-arm comparison — the
same commits, run twice:

- *Arm A, honest:* commits whose message accurately describes the fix.
- *Arm B, mislabelled:* the same diffs paired with a message that overstates them —
  a `Fixes CVE-…` subject on a commit that only renames a variable, or that
  touches the vulnerable file without changing the bounds check. The
  patch-verification service's own corpus should already contain real instances:
  a reported fix that only moved code.

The feature is trustworthy iff arm B produces `not supported` / `partly supported`
at a rate well above arm A's false-negative rate. Fifty commits per arm gives a
usable read. **Cheapest sound control:** the mislabelled arm alone. If a model shown
"Fixes CVE-1234" over a whitespace-only diff still says *Supported*, the field is
dead and no amount of arm A helps. That experiment needs the endpoint and one
afternoon, and it should gate turning this on for the service — not gate landing
the plumbing, which is inert without `--intent-context`.

A cheap structural strengthener if the numbers are bad: run the summary **twice** —
once without the claim (independent), once with it — and compare. The independent
analysis is already what the tool produces by default, so the comparison is free of
new machinery, at the cost of one extra call. Out of scope here; worth naming.

**Is `inputs.intent_shown` sufficient warning? No — necessary, not sufficient.** It
is a boolean about the input. It tells a consumer that agreement is not evidence; it
does not tell them how *much* to discount it, and a boolean invites being ignored by
the code path that reads `summary.claim` first. Concretely, a consumer should:

- Treat `intent_shown: true` + `claim: "supported"` as **weak, non-independent
  corroboration only**. It is not a pass. Never auto-close a vulnerability report on
  it.
- Treat `intent_shown: true` + `claim: "not supported"` or `"partly supported"` as a
  **strong signal**, and the one worth acting on. A model biased toward agreeing
  that contradicts the message anyway has overcome its own bias to do so; the
  asymmetry is the whole value of the field.
- Treat `claim: null` with `intent_shown: true` as **"no answer" — never as a
  pass.** It means either no claim or a malformed response, and the field alone
  cannot distinguish them.
- Read `intent_shown: false` runs the way the tool was designed: `summary.text` is
  an independent reading, and the human (or the calling agent) does the comparison
  against `commits`.

That asymmetry is the honest framing: **this field is a detector of unsupported
claims, not a certifier of supported ones.** The README now says so, and if the
service treats it as a green light rather than a red flag, this proposal has made
things worse rather than better.

## Cost

**S.** Roughly 45 lines of production code across three files, plus 43 lines of
test. No new module, no new dependency, standard library only, no 3.8+ syntax
concerns, nothing platform-specific. **Zero token cost and zero behaviour change on
the default path** — `claim_block` is rendered only when `intent` is non-empty. On
the opt-in path it adds the commit messages plus about 60 words to exactly one call.
No extra calls: the pass count stays N+2.

## Risks

- **Sycophancy makes the verdict meaningless.** The main risk, unmeasured, and the
  reason for the section above. Mitigated in framing (detector, not certifier), not
  in code.
- **Extraction silently returns `null` at scale.** If a model reliably writes
  "Partially supported" or "Mostly supported", the anchored regex yields `null`
  every time and the field looks broken rather than absent. This fails safe, but it
  fails *invisibly* — there is no counter of "asked for a verdict, got none". Worth
  watching in the first real runs.
- **`summary.claim` under `schema_version: 2`.** A new key on an unbumped version,
  pending the versioning rule. Noted in code and above.
- **Prompt/label drift.** `claim_block` and `risk.claim_label` must move together;
  a config that translates the prompt without the label silently produces `null`
  forever. Identical to the existing risk-label hazard, and documented in the same
  README section — but it is now two coupled pairs instead of one.
- **The claim is only as good as the commit messages.** A range with a hundred
  merge commits produces an `intent` blob that is not a single claim. `intent_text`
  concatenates; the model gets a wall. Fine for the `--commit REV` case this is
  aimed at, degrading for wide ranges.

## Open questions

1. **Should the verdict be surfaced in the Markdown and HTML reports as a badge?**
   Right now it appears only as the model's own `**Claim**` line inside the rendered
   summary, and as `summary.claim` in the JSON. A badge beside the risk badge would
   be four lines in `_risk_badge`'s neighbourhood — but a badge reads as
   authoritative, and this verdict is explicitly not. I left it out. The service is
   the consumer; the human reader has the sentence.
2. **Should `claim: null` be distinguishable from "no claim was made"?** Today
   `null` covers both "no `--intent-context`" and "model gave no parseable verdict",
   and the consumer must read `inputs.intent_shown` to tell them apart. A sentinel
   (`"unparsed"`) would be clearer and would let the extraction-failure rate be
   measured, at the cost of a non-null value that is not a verdict.
3. **Should the claim be given to the orientation pass too?** Pass 0 also sees the
   change whole, and it is cheap. It would bias the brief that then reaches every
   per-file call, so I did not. Worth revisiting only if the summary alone proves
   too far downstream to notice a mismatch.
4. **Should a per-file `**Claim**` line be asked for as well**, so the summary can
   reconcile N local verdicts instead of forming one global one? More calls' worth
   of signal for no extra calls — but it puts a whole-change question to a pass that
   cannot answer it, which is the defect this proposal exists to fix.
