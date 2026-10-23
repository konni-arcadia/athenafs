# Contributing

athenafs is a personal project, built and maintained with an AI coding assistant (see "How this was made" in the
README). Issues and pull requests are welcome; answers come when I have time.

- **Bugs:** open an issue with what you did, what you expected and what happened, the commit you run
  (`curl -s localhost:8080/api/status`, field `commit`), and the relevant lines of `docker logs athenafs-core`.
- **Ideas and larger changes:** open an issue first, so we can agree on the approach before you write code.
- **Pull requests:** one change per pull request, explained in its description. Keep the style of the code around it.
  Changes to the patches in `core/patches/` need a comment in `core/Dockerfile` saying why. Performance changes are
  measured with `scripts/measure.sh` (see `docs/measurements.md`).
- **Security issues:** never in a public issue: see `SECURITY.md`.
- **Licence:** contributions are accepted under the licence of the files they change (see "Licence" in the README).
- Everything in this repository is written in English.
