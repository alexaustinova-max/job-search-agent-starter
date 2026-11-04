# Third-party dependencies

The project's own code is licensed under [MIT](LICENSE). No dependency implementation,
browser binary, Node.js runtime or Python runtime is bundled in this repository.
No additional third-party source-code attribution was found in the project modules.

| Dependency | Locked version | License | Source |
| --- | --- | --- | --- |
| playwright | 1.64.1 | Apache-2.0 | [Microsoft Playwright](https://github.com/microsoft/playwright/tree/v1.64.1) |
| playwright-core | 1.64.1 | Apache-2.0 | [Microsoft Playwright](https://github.com/microsoft/playwright/tree/v1.64.1) |

These packages are installed separately by npm. Their declared license and notices were
checked against the installed locked packages and the upstream release:
[LICENSE](https://github.com/microsoft/playwright/blob/v1.64.1/LICENSE),
[NOTICE](https://github.com/microsoft/playwright/blob/v1.64.1/NOTICE).
Copies are provided in [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt) and
[licenses/Playwright-NOTICE.txt](licenses/Playwright-NOTICE.txt).
Playwright's notice also credits code derived from Puppeteer under Apache-2.0.
The project's MIT license does not replace these dependency terms.

Python standard-library modules, Node.js built-ins and macOS frameworks are used through
the user's separately installed runtimes. Google Chrome must be installed independently.
HH, OpenAI, Google Apps Script and mail providers remain external services: this repository's
license grants no account access and does not replace their terms, permissions or quotas.
