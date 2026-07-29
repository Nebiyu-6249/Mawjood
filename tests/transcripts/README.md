# Golden transcripts

Multi-turn conversations as YAML: consumer messages in, expected Mawjood
behaviour out. They are the regression net for the conversation state machine —
intent, slot filling, clarification and explicit confirmation.

Empty until Phase 2, when the conversation engine exists.

Every transcript is also subject to the no-empty-shelves blocklist: no expected
outbound message may contain failure language, in any branch, including the ones
where every upstream has failed.
