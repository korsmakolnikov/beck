"""A shell line split into its simple commands."""

from beck.shell import parts


def test_an_operator_inside_quotes_does_not_split_the_command():
    assert parts('grep -n "a\\|b" f | head -5') == ['grep -n "a\\|b" f', "head -5"]
    assert parts("echo 'x;y' && ls") == ["echo 'x;y'", "ls"]


def test_a_heredoc_s_body_is_data():
    assert parts("cat <<EOF > f\nrm -rf x\nEOF\nls") == ["cat <<EOF > f", "ls"]
