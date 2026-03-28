from hermes_cli.update_policy import resolve_update_target


def test_prod_branch_uses_fork_prod_when_fork_remote_exists():
    assert resolve_update_target("prod", {"origin", "fork"}) == ("fork", "prod")


def test_non_prod_branch_uses_origin_main():
    assert resolve_update_target("main", {"origin", "fork"}) == ("origin", "main")
    assert resolve_update_target("feature/x", {"origin", "fork"}) == ("origin", "main")


def test_prod_without_fork_falls_back_to_origin_main():
    assert resolve_update_target("prod", {"origin"}) == ("origin", "main")
