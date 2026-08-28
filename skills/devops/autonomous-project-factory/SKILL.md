---
name: autonomous-project-factory
description: Create a private GitHub repository, build one immutable GHCR image, deploy it through Dokploy, and return the live HTTPS URL.
---

# Autonomous Project Factory

Use this skill when Maikol or a team member asks to create, publish, put online,
or deploy a new project. The deployment is part of the request; do not stop after
writing code or creating a repository.

## Required flow

1. Resolve the current Hermes profile exactly. Never guess a GitHub owner.
2. Convert the requested product into a short project name and description.
3. For a simple validation project, execute:

```bash
workflow-factory create \
  --profile "$HERMES_PROFILE" \
  --name "PROJECT NAME" \
  --description "ONE-SENTENCE DESCRIPTION"
```

   For a real project, first build it under `/srv/projects`, include a
   production `Dockerfile`, and pass `--source /srv/projects/...`. If database
   migrations exist, add `.workflow-factory/migration-policy.json` declaring
   `{"strategy":"expand-contract","backward_compatible":true}`.

4. Wait for the command to finish. It is resumable and may take several minutes
   while GitHub Actions builds and Dokploy obtains HTTPS.
5. A successful result must have `stage` equal to `READY`, a private `repository`,
   an immutable `image_ref` containing `@sha256:`, and an HTTPS `url`.
6. Reply with the repository, CI run, and live URL. Explicitly invite the human to
   test the URL.

## Failure handling

- Repeat the same command to resume the same deterministic request.
- Report the exact `error` field if the CLI returns one.
- Never make the repository public.
- Never choose a fallback GitHub organization when the profile is unmapped.
- Never claim deployment completed from a green build alone; `stage=READY` is the
  success authority.
