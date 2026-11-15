# Contributing

## Release notes

Home Assistant reads `thermowatt-heater/CHANGELOG.md` alongside the app's
`config.yaml`. Keep this as the single source of release notes; the root
`CHANGELOG.md` links to it.

- Add a `## VERSION` heading matching `version` in `thermowatt-heater/config.yaml`.
- Write 1–5 short bullets describing user-visible changes for each release.
- Use generic product language and synthetic examples. Do not copy private
  installation details, device identities, locations or operational timelines.
- Run `python -m unittest discover -s tests -v` before publishing.
