# Contributing to ECHO

Thank you for contributing to ECHO! This document outlines the standards we follow.

## Commit Messages

We follow the [Conventional Commits](https://www.conventionalcommits.org/) standard for all commit messages. This leads to more readable messages that are easy to follow when looking through the project history, and allows us to generate changelogs.

Every commit message should be structured as follows:

```
<type>: <subject>
```

### Allowed Types (`<type>`)

* `feat`: A new feature
* `fix`: A bug fix
* `docs`: Documentation only changes
* `test`: Adding missing tests or correcting existing tests
* `refactor`: A code change that neither fixes a bug nor adds a feature
* `perf`: A code change that improves performance
* `ci`: Changes to our CI configuration files and scripts
* `chore`: Other changes that don't modify src or test files. **Note:** `chore:` must be specific and not used as a generic catch-all.

### Examples

**Good Examples:**

* `feat: add support for streaming responses`
* `fix: resolve timeout error during backend communication`
* `chore: bump ruff version to 0.1.5`
* `docs: add conventional commits to contributing guide`

**Bad Examples:**

* `chore: update things` (Not specific enough)
* `fix bug` (Missing type prefix)
* `wip` (Not a conventional commit)
* `chore: catch all updates` (Too broad)

### Subject (`<subject>`)

The subject contains a succinct description of the change:

* Use the imperative, present tense: "change" not "changed" nor "changes"
* Don't capitalize the first letter
* No dot (.) at the end
