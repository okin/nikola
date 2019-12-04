"""Test if section indexes avoid pages."""

import io
import os

import pytest

import nikola.plugins.command.init
from nikola import __main__
from nikola.utils import makedirs

from ..base import cd
from .helper import append_config
from .test_empty_build import test_archive_exists  # NOQA
from .test_demo_build import (  # NOQA
    test_index_in_sitemap, test_avoid_double_slash_in_rss)


def test_section_index_avoidance(build, output_dir):
    """Test section index."""

    def _make_output_path(dir, name):
        """Make a file path to the output."""
        return os.path.join(dir, name + '.html')

    sec1 = os.path.join(output_dir, "sec1")
    foo = os.path.join(output_dir, "sec1", "post0")

    # Do all files exist?
    assert os.path.isfile(_make_output_path(sec1, 'index'))
    assert os.path.isfile(_make_output_path(foo, 'index'))

    # Is it really a page?
    with io.open(os.path.join(sec1, 'index.html'), 'r', encoding='utf-8') as fh:
        page = fh.read()

    assert 'This is Page 0' in page
    assert 'This is Post 0' not in page


@pytest.fixture(scope="module")
def build(target_dir):
    """
    Add subdirectories and create a post in section "sec1" and a page
    with the same URL as the section index.

    It also enables post sections.
    """
    init_command = nikola.plugins.command.init.CommandInit()
    init_command.create_empty_site(target_dir)
    init_command.create_configuration(target_dir)

    pages = os.path.join(target_dir, "pages")
    posts = os.path.join(target_dir, "posts")
    sec1 = os.path.join(posts, "sec1")

    makedirs(pages)
    makedirs(sec1)

    with io.open(os.path.join(pages, 'sec1.txt'), "w+", encoding="utf8") as outf:
        outf.write(".. title: Page 0\n.. slug: sec1\n\nThis is Page 0.\n")

    with io.open(os.path.join(sec1, 'foo.txt'), "w+", encoding="utf8") as outf:
        outf.write(
            ".. title: Post 0\n.. slug: post0\n.. date: 2013-03-06 19:08:15\n\nThis is Post 0.\n")

    append_config(target_dir, """
POSTS_SECTIONS = True
POSTS_SECTIONS_ARE_INDEXES = True
PRETTY_URLS = True
POSTS = (('posts/*.txt', '', 'post.tmpl'),)
PAGES = (('pages/*.txt', '', 'page.tmpl'),)
""")

    with cd(target_dir):
        __main__.main(["build"])