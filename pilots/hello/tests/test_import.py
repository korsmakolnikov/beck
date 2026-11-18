import hello


def test_the_package_imports() -> None:
    assert hello.__name__ == "hello"
