from cc_autopilot_notify.project import project_name


def test_last_segment_outside_home():
    assert project_name("/var/www/demo", home="/home/u") == "demo"


def test_empty_cwd_is_unknown():
    assert project_name("", home="/home/u") == "unknown"
    assert project_name("   ", home="/home/u") == "unknown"


def test_dot_and_dotdot_and_duplicate_slashes_fold():
    assert project_name("//var/www/./demo/../other/", home="/home/u") == "other"


def test_parent_of_root_does_not_escape():
    assert project_name("/..", home="/home/u") == "unknown"


def test_worktree_resolves_to_the_repository_root():
    assert project_name("/var/www/demo/.claude/worktrees/fix-x", home="/home/u") == "demo"
    assert project_name("/var/www/demo/.worktrees/fix-x", home="/home/u") == "demo"


def test_a_similar_name_is_not_treated_as_a_worktree():
    assert project_name("/var/www/demo/.claude/worktrees-extra/x", home="/home/u") == "x"


def test_home_renders_as_a_tilde():
    assert project_name("/home/u", home="/home/u") == "~"
    assert project_name("/home/u/code/demo", home="/home/u") == "~/code/demo"


def test_deep_home_path_drops_the_middle():
    assert project_name("/home/u/a/b/c/d", home="/home/u") == "~/a/b/…/d"


def test_windows_separators_fold():
    assert project_name(r"C:\Users\u\demo", home=r"C:\Users\u") == "~/demo"


def test_symlinks_are_not_resolved(tmp_path):
    """A cwd may point at nothing (a removed worktree); realpath would lie."""
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing")
    assert project_name(str(link / "a" / "b" / "c"), home="/home/u") == "c"
