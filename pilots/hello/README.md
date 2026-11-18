# hello

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (`make deps` installs them on Arch Linux and Ubuntu).

```sh
make install    # create the environment and install the dependencies
make test       # pytest (with Hypothesis for property-based tests)
make check      # ruff format --check, ruff check, mypy --strict
make format     # format the code
```
