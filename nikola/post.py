# -*- coding: utf-8 -*-

# Copyright © 2012-2019 Roberto Alsina and others.

# Permission is hereby granted, free of charge, to any
# person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the
# Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the
# Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice
# shall be included in all copies or substantial portions of
# the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
# OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""The Post class."""

import io
import datetime
import hashlib
import json
import os
import re
from collections import defaultdict
from math import ceil  # for reading time feature
from urllib.parse import urljoin

import dateutil.tz
import lxml.html
import natsort
from blinker import signal

# for tearDown with _reload we cannot use 'from import' to get forLocaleBorg
import nikola.utils
from . import metadata_extractors
from . import utils
from .utils import (
    current_time,
    Functionary,
    LOGGER,
    LocaleBorg,
    slugify,
    to_datetime,
    unicode_str,
    demote_headers,
    get_translation_candidate,
    map_metadata
)

try:
    import pyphen
except ImportError:
    pyphen = None


__all__ = ('Post',)

TEASER_REGEXP = re.compile(r'<!--\s*(TEASER_END|END_TEASER)(:(.+))?\s*-->', re.IGNORECASE)


class Post(object):
    """Represent a blog post or site page."""

    def __init__(
        self,
        source_path,
        config,
        destination,
        use_in_feeds,
        messages,
        template_name,
        compiler,
        destination_base=None,
        metadata_extractors_by=None
    ):
        """Initialize post.

        The source path is the user created post file. From it we calculate
        the meta file, as well as any translations available, and
        the .html fragment file path.

        destination_base must be None or a TranslatableSetting instance. If
        specified, it will be prepended to the destination path.
        """
        self.config = config
        self.compiler = compiler
        self.compiler_contexts = {}
        self.compile_html = self.compiler.compile
        self.demote_headers = self.compiler.demote_headers and self.config['DEMOTE_HEADERS']
        tzinfo = self.config['__tzinfo__']
        if self.config['FUTURE_IS_NOW']:
            self.current_time = None
        else:
            self.current_time = current_time(tzinfo)
        self.translated_to = set([])
        self._prev_post = None
        self._next_post = None
        self.base_url = self.config['BASE_URL']
        self.is_draft = False
        self.is_private = False
        self.strip_indexes = self.config['STRIP_INDEXES']
        self.index_file = self.config['INDEX_FILE']
        self.pretty_urls = self.config['PRETTY_URLS']
        self.source_path = source_path  # posts/blah.txt
        self.post_name = os.path.splitext(source_path)[0]  # posts/blah
        _relpath = os.path.relpath(self.post_name)
        if _relpath != self.post_name:
            self.post_name = _relpath.replace('..' + os.sep, '_..' + os.sep)
        # cache[\/]posts[\/]blah.html
        self.base_path = os.path.join(self.config['CACHE_FOLDER'], self.post_name + ".html")
        # cache/posts/blah.html
        self._base_path = self.base_path.replace('\\', '/')
        self.metadata_path = self.post_name + ".meta"  # posts/blah.meta
        self.folder_relative = destination
        self.folder_base = destination_base
        self.default_lang = self.config['DEFAULT_LANG']
        self.translations = self.config['TRANSLATIONS']
        self.messages = messages
        self.skip_untranslated = not self.config['SHOW_UNTRANSLATED_POSTS']
        self._template_name = template_name
        self.is_two_file = True
        self._reading_time = None
        self._remaining_reading_time = None
        self._paragraph_count = None
        self._remaining_paragraph_count = None
        self._dependency_file_fragment = defaultdict(list)
        self._dependency_file_page = defaultdict(list)
        self._dependency_uptodate_fragment = defaultdict(list)
        self._dependency_uptodate_page = defaultdict(list)
        self._depfile = defaultdict(list)
        if metadata_extractors_by is None:
            self.metadata_extractors_by = {'priority': {}, 'source': {}}
        else:
            self.metadata_extractors_by = metadata_extractors_by

        # Load internationalized metadata
        for lang in self.translations:
            if os.path.isfile(get_translation_candidate(self.config, self.source_path, lang)):
                self.translated_to.add(lang)

        default_metadata, default_used_extractor = get_meta(self, lang=None)

        self.meta = Functionary(lambda: None, self.default_lang)
        self.used_extractor = Functionary(lambda: None, self.default_lang)
        self.meta[self.default_lang] = default_metadata
        self.used_extractor[self.default_lang] = default_used_extractor

        # Compose paths
        if self.folder_base is not None:
            # Use translatable destination folders
            self.folders = {}
            for lang in self.config['TRANSLATIONS'].keys():
                if os.path.isabs(self.folder_base(lang)):  # Issue 2982
                    self.folder_base[lang] = os.path.relpath(self.folder_base(lang), '/')
                self.folders[lang] = os.path.normpath(os.path.join(self.folder_base(lang), self.folder_relative))
        else:
            # Old behavior (non-translatable destination path, normalized by scanner)
            self.folders = {lang: self.folder_relative for lang in self.config['TRANSLATIONS'].keys()}
        self.folder = self.folders[self.default_lang]

        if 'date' not in default_metadata and not use_in_feeds:
            # For pages we don't *really* need a date
            if self.config['__invariant__']:
                default_metadata['date'] = datetime.datetime(2013, 12, 31, 23, 59, 59, tzinfo=tzinfo)
            else:
                default_metadata['date'] = datetime.datetime.utcfromtimestamp(
                    os.stat(self.source_path).st_ctime).replace(tzinfo=dateutil.tz.tzutc()).astimezone(tzinfo)

        # If time zone is set, build localized datetime.
        try:
            self.date = to_datetime(self.meta[self.default_lang]['date'], tzinfo)
        except ValueError:
            if not self.meta[self.default_lang]['date']:
                msg = 'Missing date in file {}'.format(source_path)
            else:
                msg = "Invalid date '{0}' in file {1}".format(self.meta[self.default_lang]['date'], source_path)
            LOGGER.error(msg)
            raise ValueError(msg)

        if 'updated' not in default_metadata:
            default_metadata['updated'] = default_metadata.get('date', None)

        self.updated = to_datetime(default_metadata['updated'], tzinfo)

        if 'title' not in default_metadata or 'slug' not in default_metadata \
                or 'date' not in default_metadata:
            raise ValueError("You must set a title (found '{0}'), a slug (found '{1}') and a date (found '{2}')! "
                             "[in file {3}]".format(default_metadata.get('title', None),
                                                    default_metadata.get('slug', None),
                                                    default_metadata.get('date', None),
                                                    source_path))

        if 'type' not in default_metadata:
            # default value is 'text'
            default_metadata['type'] = 'text'

        for lang in self.translations:
            if lang != self.default_lang:
                meta = defaultdict(lambda: '')
                meta.update(default_metadata)
                _meta, _extractors = get_meta(self, lang)
                meta.update(_meta)
                self.meta[lang] = meta
                self.used_extractor[lang] = _extractors

        if not self.is_translation_available(self.default_lang):
            # Special case! (Issue #373)
            # Fill default_metadata with stuff from the other languages
            for lang in sorted(self.translated_to):
                default_metadata.update(self.meta[lang])

        # Load data field from metadata
        self.data = Functionary(lambda: None, self.default_lang)
        for lang in self.translations:
            if self.meta[lang].get('data') is not None:
                self.data[lang] = utils.load_data(self.meta[lang]['data'])

        for lang, meta in self.meta.items():
            # Migrate section to category
            # TODO: remove in v9
            if 'section' in meta:
                if 'category' in meta:
                    LOGGER.warning("Post {0} has both 'category' and 'section' metadata. Section will be ignored.".format(source_path))
                else:
                    meta['category'] = meta['section']
                    LOGGER.info("Post {0} uses 'section' metadata, setting its value to 'category'".format(source_path))

            # Handle CATEGORY_DESTPATH_AS_DEFAULT
            if 'category' not in meta and self.config['CATEGORY_DESTPATH_AS_DEFAULT']:
                self.category_from_destpath = True
                if self.config['CATEGORY_DESTPATH_TRIM_PREFIX'] and self.folder_relative != '.':
                    category = self.folder_relative
                else:
                    category = self.folders[lang]
                category = category.replace(os.sep, '/')
                if self.config['CATEGORY_DESTPATH_FIRST_DIRECTORY_ONLY']:
                    category = category.split('/')[0]
                meta['category'] = self.config['CATEGORY_DESTPATH_NAMES'](lang).get(category, category)
            else:
                self.category_from_destpath = False

        self.publish_later = False if self.current_time is None else self.date >= self.current_time

        self.is_draft = False
        self.is_private = False
        self.post_status = 'published'
        self._tags = {}
        self.has_oldstyle_metadata_tags = False
        for lang in self.translated_to:
            if isinstance(self.meta[lang]['tags'], (list, tuple, set)):
                _tag_list = self.meta[lang]['tags']
            else:
                _tag_list = self.meta[lang]['tags'].split(',')
            self._tags[lang] = natsort.natsorted(
                list(set([x.strip() for x in _tag_list])),
                alg=natsort.ns.F | natsort.ns.IC)
            self._tags[lang] = [t for t in self._tags[lang] if t]

            status = self.meta[lang].get('status')
            if status:
                if status == 'published':
                    pass  # already set before, mixing published + something else should result in the other thing
                elif status == 'featured':
                    self.post_status = status
                elif status == 'private':
                    self.post_status = status
                    self.is_private = True
                elif status == 'draft':
                    self.post_status = status
                    self.is_draft = True
                else:
                    LOGGER.warning(('The post "{0}" has the unknown status "{1}". '
                                    'Valid values are "published", "featured", "private" and "draft".').format(self.source_path, status))

            if self.config['WARN_ABOUT_TAG_METADATA']:
                show_warning = False
                if 'draft' in [_.lower() for _ in self._tags[lang]]:
                    LOGGER.warning('The post "{0}" uses the "draft" tag.'.format(self.source_path))
                    show_warning = True
                if 'private' in self._tags[lang]:
                    LOGGER.warning('The post "{0}" uses the "private" tag.'.format(self.source_path))
                    show_warning = True
                if 'mathjax' in self._tags[lang]:
                    LOGGER.warning('The post "{0}" uses the "mathjax" tag.'.format(self.source_path))
                    show_warning = True
                if show_warning:
                    LOGGER.warning('It is suggested that you convert special tags to metadata and set '
                                   'USE_TAG_METADATA to False. You can use the upgrade_metadata_v8 '
                                   'command plugin for conversion (install with: nikola plugin -i '
                                   'upgrade_metadata_v8). Change the WARN_ABOUT_TAG_METADATA '
                                   'configuration to disable this warning.')
            if self.config['USE_TAG_METADATA']:
                if 'draft' in [_.lower() for _ in self._tags[lang]]:
                    self.is_draft = True
                    LOGGER.debug('The post "{0}" is a draft.'.format(self.source_path))
                    self._tags[lang].remove('draft')
                    self.post_status = 'draft'
                    self.has_oldstyle_metadata_tags = True

                if 'private' in self._tags[lang]:
                    self.is_private = True
                    LOGGER.debug('The post "{0}" is private.'.format(self.source_path))
                    self._tags[lang].remove('private')
                    self.post_status = 'private'
                    self.has_oldstyle_metadata_tags = True

                if 'mathjax' in self._tags[lang]:
                    self.has_oldstyle_metadata_tags = True

        # While draft comes from the tags, it's not really a tag
        self.is_post = use_in_feeds
        self.use_in_feeds = self.is_post and not self.is_draft and not self.is_private and not self.publish_later

        # Allow overriding URL_TYPE via meta
        # The check is done here so meta dicts won’t change inside of
        # generic_post_rendere
        self.url_type = self.meta('url_type') or None
        # Register potential extra dependencies
        self.compiler.register_extra_dependencies(self)

    def _get_hyphenate(self):
        return bool(self.config['HYPHENATE'] or self.meta('hyphenate'))

    hyphenate = property(_get_hyphenate)

    def __repr__(self):
        """Provide a representation of the post object."""
        # Calculate a hash that represents most data about the post
        m = hashlib.md5()
        # source_path modification date (to avoid reading it)
        m.update(utils.unicode_str(os.stat(self.source_path).st_mtime).encode('utf-8'))
        clean_meta = {}
        for k, v in self.meta.items():
            sub_meta = {}
            clean_meta[k] = sub_meta
            for kk, vv in v.items():
                if vv:
                    sub_meta[kk] = vv
        m.update(utils.unicode_str(json.dumps(clean_meta, cls=utils.CustomEncoder, sort_keys=True)).encode('utf-8'))
        return '<Post: {0!r} {1}>'.format(self.source_path, m.hexdigest())

    def has_pretty_url(self, lang):
        """Check if this page has a pretty URL."""
        m = self.meta[lang].get('pretty_url', '')
        if m:
            # match is a non-empty string, overides anything
            return m == 'True'
        else:
            # use PRETTY_URLS, unless the slug is 'index'
            return self.pretty_urls and self.meta[lang]['slug'] != 'index'

    def _has_pretty_url(self, lang):
        """Check if this page has a pretty URL."""
        return self.has_pretty_url(lang)

    @property
    def has_math(self):
        """Return True if this post has has_math set to True or is a python notebook.

        Alternatively, it will return True if it has set the mathjax tag in the
        current language and the USE_TAG_METADATA config setting is True.
        """
        if self.compiler.name == 'ipynb':
            return True
        lang = nikola.utils.LocaleBorg().current_lang
        if self.is_translation_available(lang):
            if self.meta[lang].get('has_math') in ('true', 'True', 'yes', '1', 1, True):
                return True
            if self.config['USE_TAG_METADATA']:
                return 'mathjax' in self.tags_for_language(lang)
        # If it has math in ANY other language, enable it. Better inefficient than broken.
        for lang in self.translated_to:
            if self.meta[lang].get('has_math') in ('true', 'True', 'yes', '1', 1, True):
                return True
        if self.config['USE_TAG_METADATA']:
            return 'mathjax' in self.alltags
        return False

    @property
    def alltags(self):
        """Return ALL the tags for this post."""
        tags = []
        for l in self._tags:
            tags.extend(self._tags[l])
        return list(set(tags))

    def tags_for_language(self, lang):
        """Return tags for a given language."""
        if lang in self._tags:
            return self._tags[lang]
        elif lang not in self.translated_to and self.skip_untranslated:
            return []
        elif self.default_lang in self._tags:
            return self._tags[self.default_lang]
        else:
            return []

    @property
    def tags(self):
        """Return tags for the current language."""
        lang = nikola.utils.LocaleBorg().current_lang
        return self.tags_for_language(lang)

    @property
    def prev_post(self):
        """Return previous post."""
        lang = nikola.utils.LocaleBorg().current_lang
        rv = self._prev_post
        while self.skip_untranslated:
            if rv is None:
                break
            if rv.is_translation_available(lang):
                break
            rv = rv._prev_post
        return rv

    @prev_post.setter
    def prev_post(self, v):
        """Set previous post."""
        self._prev_post = v

    @property
    def next_post(self):
        """Return next post."""
        lang = nikola.utils.LocaleBorg().current_lang
        rv = self._next_post
        while self.skip_untranslated:
            if rv is None:
                break
            if rv.is_translation_available(lang):
                break
            rv = rv._next_post
        return rv

    @next_post.setter
    def next_post(self, v):
        """Set next post."""
        self._next_post = v

    @property
    def template_name(self):
        """Return template name for this post."""
        lang = nikola.utils.LocaleBorg().current_lang
        return self.meta[lang]['template'] or self._template_name

    def formatted_date(self, date_format, date=None):
        """Return the formatted date as string."""
        return utils.LocaleBorg().formatted_date(date_format, date if date else self.date)

    def formatted_updated(self, date_format):
        """Return the updated date as string."""
        return self.formatted_date(date_format, self.updated)

    def title(self, lang=None):
        """Return localized title.

        If lang is not specified, it defaults to the current language from
        templates, as set in LocaleBorg.
        """
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang
        return self.meta[lang]['title']

    def author(self, lang=None):
        """Return localized author or BLOG_AUTHOR if unspecified.

        If lang is not specified, it defaults to the current language from
        templates, as set in LocaleBorg.
        """
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang
        if self.meta[lang]['author']:
            author = self.meta[lang]['author']
        else:
            author = self.config['BLOG_AUTHOR'](lang)

        return author

    def description(self, lang=None):
        """Return localized description."""
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang
        return self.meta[lang]['description']

    def guid(self, lang=None):
        """Return localized GUID."""
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang
        if self.meta[lang]['guid']:
            guid = self.meta[lang]['guid']
        else:
            guid = self.permalink(lang, absolute=True)

        return guid

    def add_dependency(self, dependency, add='both', lang=None):
        """Add a file dependency for tasks using that post.

        The ``dependency`` should be a string specifying a path, or a callable
        which returns such a string or a list of strings.

        The ``add`` parameter can be 'both', 'fragment' or 'page', to indicate
        that this dependency shall be used
         * when rendering the fragment to HTML ('fragment' and 'both'), or
         * when creating a page with parts of the ``Post`` embedded, which
           includes the HTML resulting from compiling the fragment ('page' or
           'both').

        If ``lang`` is not specified, this dependency is added for all languages.
        """
        if add not in {'fragment', 'page', 'both'}:
            raise Exception("Add parameter is '{0}', but must be either 'fragment', 'page', or 'both'.".format(add))
        if add == 'fragment' or add == 'both':
            self._dependency_file_fragment[lang].append((type(dependency) != str, dependency))
        if add == 'page' or add == 'both':
            self._dependency_file_page[lang].append((type(dependency) != str, dependency))

    def add_dependency_uptodate(self, dependency, is_callable=False, add='both', lang=None):
        """Add a dependency for task's ``uptodate`` for tasks using that post.

        This can be for example an ``utils.config_changed`` object, or a list of
        such objects.

        The ``is_callable`` parameter specifies whether ``dependency`` is a
        callable which generates an entry or a list of entries for the ``uptodate``
        list, or whether it is an entry which can directly be added (as a single
        object or a list of objects).

        The ``add`` parameter can be 'both', 'fragment' or 'page', to indicate
        that this dependency shall be used
         * when rendering the fragment to HTML ('fragment' and 'both'), or
         * when creating a page with parts of the ``Post`` embedded, which
           includes the HTML resulting from compiling the fragment ('page' or
           'both').

        If ``lang`` is not specified, this dependency is added for all languages.

        Example:

        post.add_dependency_uptodate(
            utils.config_changed({1: some_data}, 'uniqueid'), False, 'page')
        """
        if add == 'fragment' or add == 'both':
            self._dependency_uptodate_fragment[lang].append((is_callable, dependency))
        if add == 'page' or add == 'both':
            self._dependency_uptodate_page[lang].append((is_callable, dependency))

    def register_depfile(self, dep, dest=None, lang=None):
        """Register a dependency in the dependency file."""
        if not dest:
            dest = self.translated_base_path(lang)
        self._depfile[dest].append(dep)

    @staticmethod
    def write_depfile(dest, deps_list, post=None, lang=None):
        """Write a depfile for a given language."""
        if post is None or lang is None:
            deps_path = dest + '.dep'
        else:
            deps_path = post.compiler.get_dep_filename(post, lang)
        if deps_list or (post.compiler.use_dep_file if post else False):
            deps_list = [p for p in deps_list if p != dest]  # Don't depend on yourself (#1671)
            with io.open(deps_path, "w+", encoding="utf8") as deps_file:
                deps_file.write('\n'.join(deps_list))
        else:
            if os.path.isfile(deps_path):
                os.unlink(deps_path)

    def _get_dependencies(self, deps_list):
        deps = []
        for dep in deps_list:
            if dep[0]:
                # callable
                result = dep[1]()
            else:
                # can add directly
                result = dep[1]
            # if result is a list, add its contents
            if type(result) == list:
                deps.extend(result)
            else:
                deps.append(result)
        return deps

    def deps(self, lang):
        """Return a list of file dependencies to build this post's page."""
        deps = []
        deps.append(self.base_path)
        deps.append(self.source_path)
        if os.path.exists(self.metadata_path):
            deps.append(self.metadata_path)
        if lang != self.default_lang:
            cand_1 = get_translation_candidate(self.config, self.source_path, lang)
            cand_2 = get_translation_candidate(self.config, self.base_path, lang)
            if os.path.exists(cand_1):
                deps.extend([cand_1, cand_2])
            cand_3 = get_translation_candidate(self.config, self.metadata_path, lang)
            if os.path.exists(cand_3):
                deps.append(cand_3)
        if self.meta('data', lang):
            deps.append(self.meta('data', lang))
        deps += self._get_dependencies(self._dependency_file_page[lang])
        deps += self._get_dependencies(self._dependency_file_page[None])
        return sorted(set(deps))

    def deps_uptodate(self, lang):
        """Return a list of uptodate dependencies to build this post's page.

        These dependencies should be included in ``uptodate`` for the task
        which generates the page.
        """
        deps = []
        deps += self._get_dependencies(self._dependency_uptodate_page[lang])
        deps += self._get_dependencies(self._dependency_uptodate_page[None])
        deps.append(utils.config_changed({1: sorted(self.compiler.config_dependencies)}, 'nikola.post.Post.deps_uptodate:compiler:' + self.source_path))
        return deps

    def compile(self, lang):
        """Generate the cache/ file with the compiled post."""
        dest = self.translated_base_path(lang)
        if not self.is_translation_available(lang) and not self.config['SHOW_UNTRANSLATED_POSTS']:
            return
        # Set the language to the right thing
        LocaleBorg().set_locale(lang)
        self.compile_html(
            self.translated_source_path(lang),
            dest,
            self.is_two_file,
            self,
            lang)
        Post.write_depfile(dest, self._depfile[dest], post=self, lang=lang)

        signal('compiled').send({
            'source': self.translated_source_path(lang),
            'dest': dest,
            'post': self,
            'lang': lang,
        })

        if self.publish_later:
            LOGGER.info('{0} is scheduled to be published in the future ({1})'.format(
                self.source_path, self.date))

    def fragment_deps(self, lang):
        """Return a list of dependencies to build this post's fragment."""
        deps = [self.source_path]
        if os.path.isfile(self.metadata_path):
            deps.append(self.metadata_path)
        lang_deps = []
        if lang != self.default_lang:
            lang_deps = [get_translation_candidate(self.config, d, lang) for d in deps]
            deps += lang_deps
        deps = [d for d in deps if os.path.exists(d)]
        deps += self._get_dependencies(self._dependency_file_fragment[lang])
        deps += self._get_dependencies(self._dependency_file_fragment[None])
        return sorted(deps)

    def fragment_deps_uptodate(self, lang):
        """Return a list of file dependencies to build this post's fragment."""
        deps = []
        deps += self._get_dependencies(self._dependency_uptodate_fragment[lang])
        deps += self._get_dependencies(self._dependency_uptodate_fragment[None])
        deps.append(utils.config_changed({1: sorted(self.compiler.config_dependencies)}, 'nikola.post.Post.deps_uptodate:compiler:' + self.source_path))
        return deps

    def is_translation_available(self, lang):
        """Return True if the translation actually exists."""
        return lang in self.translated_to

    def translated_source_path(self, lang):
        """Return path to the translation's source file."""
        if lang in self.translated_to:
            if lang == self.default_lang:
                return self.source_path
            else:
                return get_translation_candidate(self.config, self.source_path, lang)
        elif lang != self.default_lang:
            return self.source_path
        else:
            return get_translation_candidate(self.config, self.source_path, sorted(self.translated_to)[0])

    def translated_base_path(self, lang):
        """Return path to the translation's base_path file."""
        return get_translation_candidate(self.config, self.base_path, lang)

    def _translated_file_path(self, lang):
        """Return path to the translation's file, or to the original."""
        if lang in self.translated_to:
            if lang == self.default_lang:
                return self.base_path
            else:
                return get_translation_candidate(self.config, self.base_path, lang)
        elif lang != self.default_lang:
            return self.base_path
        else:
            return get_translation_candidate(self.config, self.base_path, sorted(self.translated_to)[0])

    def text(self, lang=None, teaser_only=False, strip_html=False, show_read_more_link=True,
             feed_read_more_link=False, feed_links_append_query=None):
        """Read the post file for that language and return its contents.

        teaser_only=True breaks at the teaser marker and returns only the teaser.
        strip_html=True removes HTML tags
        show_read_more_link=False does not add the Read more... link
        feed_read_more_link=True uses FEED_READ_MORE_LINK instead of INDEX_READ_MORE_LINK
        lang=None uses the last used to set locale

        All links in the returned HTML will be relative.
        The HTML returned is a bare fragment, not a full document.
        """
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang
        file_name = self._translated_file_path(lang)

        # Yes, we compile it and screw it.
        # This may be controversial, but the user (or someone) is asking for the post text
        # and the post should not just refuse to give it.
        if not os.path.isfile(file_name):
            self.compile(lang)

        with io.open(file_name, "r", encoding="utf8") as post_file:
            data = post_file.read().strip()

        if self.compiler.extension() == '.php':
            return data
        try:
            document = lxml.html.fragment_fromstring(data, "body")
        except lxml.etree.ParserError as e:
            # if we don't catch this, it breaks later (Issue #374)
            if str(e) == "Document is empty":
                return ""
            # let other errors raise
            raise
        base_url = self.permalink(lang=lang)
        document.make_links_absolute(base_url)

        if self.hyphenate:
            hyphenate(document, lang)

        try:
            data = lxml.html.tostring(document.body, encoding='unicode')
        except Exception:
            data = lxml.html.tostring(document, encoding='unicode')

        if teaser_only:
            teaser_regexp = self.config.get('TEASER_REGEXP', TEASER_REGEXP)
            teaser = teaser_regexp.split(data)[0]
            if teaser != data:
                if not strip_html and show_read_more_link:
                    if teaser_regexp.search(data).groups()[-1]:
                        teaser_text = teaser_regexp.search(data).groups()[-1]
                    else:
                        teaser_text = self.messages[lang]["Read more"]
                    l = self.config['FEED_READ_MORE_LINK'](lang) if feed_read_more_link else self.config['INDEX_READ_MORE_LINK'](lang)
                    teaser += l.format(
                        link=self.permalink(lang, query=feed_links_append_query),
                        read_more=teaser_text,
                        min_remaining_read=self.messages[lang]["%d min remaining to read"] % (self.remaining_reading_time),
                        reading_time=self.reading_time,
                        remaining_reading_time=self.remaining_reading_time,
                        paragraph_count=self.paragraph_count,
                        remaining_paragraph_count=self.remaining_paragraph_count,
                        post_title=self.title(lang))
                # This closes all open tags and sanitizes the broken HTML
                document = lxml.html.fromstring(teaser)
                try:
                    data = lxml.html.tostring(document.body, encoding='unicode')
                except IndexError:
                    data = lxml.html.tostring(document, encoding='unicode')

        if data and strip_html:
            try:
                # Not all posts have a body. For example, you may have a page statically defined in the template that does not take content as input.
                content = lxml.html.fromstring(data)
                data = content.text_content().strip()  # No whitespace wanted.
            except (lxml.etree.ParserError, ValueError):
                data = ""
        elif data:
            if self.demote_headers:
                # see above
                try:
                    document = lxml.html.fromstring(data)
                    demote_headers(document, self.demote_headers)
                    data = lxml.html.tostring(document.body, encoding='unicode')
                except (lxml.etree.ParserError, IndexError):
                    data = lxml.html.tostring(document, encoding='unicode')

        return data

    @property
    def reading_time(self):
        """Return reading time based on length of text."""
        if self._reading_time is None:
            text = self.text(strip_html=True)
            words_per_minute = 220
            words = len(text.split())
            markup = lxml.html.fromstring(self.text(strip_html=False))
            embeddables = [".//img", ".//picture", ".//video", ".//audio", ".//object", ".//iframe"]
            media_time = 0
            for embedded in embeddables:
                media_time += (len(markup.findall(embedded)) * 0.33)  # +20 seconds
            self._reading_time = int(ceil((words / words_per_minute) + media_time)) or 1
        return self._reading_time

    @property
    def remaining_reading_time(self):
        """Remaining reading time based on length of text (does not include teaser)."""
        if self._remaining_reading_time is None:
            text = self.text(teaser_only=True, strip_html=True)
            words_per_minute = 220
            words = len(text.split())
            self._remaining_reading_time = self.reading_time - int(ceil(words / words_per_minute)) or 1
        return self._remaining_reading_time

    @property
    def paragraph_count(self):
        """Return the paragraph count for this post."""
        if self._paragraph_count is None:
            # duplicated with Post.text()
            lang = nikola.utils.LocaleBorg().current_lang
            file_name = self._translated_file_path(lang)
            with io.open(file_name, "r", encoding="utf8") as post_file:
                data = post_file.read().strip()
            try:
                document = lxml.html.fragment_fromstring(data, "body")
            except lxml.etree.ParserError as e:
                # if we don't catch this, it breaks later (Issue #374)
                if str(e) == "Document is empty":
                    return ""
                # let other errors raise
                raise

            # output is a float, for no real reason at all
            self._paragraph_count = int(document.xpath('count(//p)'))
        return self._paragraph_count

    @property
    def remaining_paragraph_count(self):
        """Return the remaining paragraph count for this post (does not include teaser)."""
        if self._remaining_paragraph_count is None:
            try:
                # Just asking self.text() is easier here.
                document = lxml.html.fragment_fromstring(self.text(teaser_only=True, show_read_more_link=False), "body")
            except lxml.etree.ParserError as e:
                # if we don't catch this, it breaks later (Issue #374)
                if str(e) == "Document is empty":
                    return ""
                # let other errors raise
                raise

            self._remaining_paragraph_count = self.paragraph_count - int(document.xpath('count(//p)'))
        return self._remaining_paragraph_count

    def source_link(self, lang=None):
        """Return absolute link to the post's source."""
        ext = self.source_ext(True)
        link = "/" + self.destination_path(lang=lang, extension=ext, sep='/')
        link = utils.encodelink(link)
        return link

    def destination_path(self, lang=None, extension='.html', sep=os.sep):
        """Destination path for this post, relative to output/.

        If lang is not specified, it's the current language.
        Extension is used in the path if specified.
        """
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang
        folder = self.folders[lang]
        if self.has_pretty_url(lang):
            path = os.path.join(self.translations[lang],
                                folder, self.meta[lang]['slug'], 'index' + extension)
        else:
            path = os.path.join(self.translations[lang],
                                folder, self.meta[lang]['slug'] + extension)
        if sep != os.sep:
            path = path.replace(os.sep, sep)
        if path.startswith('./'):
            path = path[2:]
        return path

    def permalink(self, lang=None, absolute=False, extension='.html', query=None):
        """Return permalink for a post."""
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang

        # Let compilers override extension (e.g. the php compiler)
        if self.compiler.extension() != '.html':
            extension = self.compiler.extension()

        pieces = self.translations[lang].split(os.sep)
        pieces += self.folders[lang].split(os.sep)
        if self.has_pretty_url(lang):
            pieces += [self.meta[lang]['slug'], 'index' + extension]
        else:
            pieces += [self.meta[lang]['slug'] + extension]
        pieces = [_f for _f in pieces if _f and _f != '.']
        link = '/' + '/'.join(pieces)
        if absolute:
            link = urljoin(self.base_url, link[1:])
        index_len = len(self.index_file)
        if self.strip_indexes and link[-(1 + index_len):] == '/' + self.index_file:
            link = link[:-index_len]
        if query:
            link = link + "?" + query
        link = utils.encodelink(link)
        return link

    @property
    def previewimage(self, lang=None):
        """Return the previewimage path."""
        if lang is None:
            lang = nikola.utils.LocaleBorg().current_lang

        image_path = self.meta[lang]['previewimage']

        if not image_path:
            return None

        # This is further parsed by the template, because we don’t have access
        # to the URL replacer here.  (Issue #1473)
        return image_path

    def source_ext(self, prefix=False):
        """Return the source file extension.

        If `prefix` is True, a `.src.` prefix will be added to the resulting extension
        if it's equal to the destination extension.
        """
        ext = os.path.splitext(self.source_path)[1]
        # do not publish PHP sources
        if prefix and ext == '.html':
            # ext starts with a dot
            return '.src' + ext
        else:
            return ext


def get_metadata_from_file(source_path, post, config, lang, metadata_extractors_by):
    """Extract metadata from the file itself, by parsing contents."""
    try:
        if lang and config:
            source_path = get_translation_candidate(config, source_path, lang)
        elif lang:
            source_path += '.' + lang
        with io.open(source_path, "r", encoding="utf-8-sig") as meta_file:
            source_text = meta_file.read()
    except (UnicodeDecodeError, UnicodeEncodeError):
        msg = 'Error reading {0}: Nikola only supports UTF-8 files'.format(source_path)
        LOGGER.error(msg)
        raise ValueError(msg)
    except Exception:  # The file may not exist, for multilingual sites
        return {}, None

    meta = {}
    used_extractor = None
    for priority in metadata_extractors.MetaPriority:
        found_in_priority = False
        for extractor in metadata_extractors_by['priority'].get(priority, []):
            if not metadata_extractors.check_conditions(post, source_path, extractor.conditions, config, source_text):
                continue
            extractor.check_requirements()
            new_meta = extractor.extract_text(source_text)
            if new_meta:
                found_in_priority = True
                used_extractor = extractor
                # Map metadata from other platforms to names Nikola expects (Issue #2817)
                # Map metadata values (Issue #3025)
                map_metadata(new_meta, extractor.map_from, config)

                meta.update(new_meta)
                break

        if found_in_priority:
            break
    return meta, used_extractor


def get_metadata_from_meta_file(path, post, config, lang, metadata_extractors_by=None):
    """Take a post path, and gets data from a matching .meta file."""
    meta_path = os.path.splitext(path)[0] + '.meta'
    if lang and config:
        meta_path = get_translation_candidate(config, meta_path, lang)
    elif lang:
        meta_path += '.' + lang
    if os.path.isfile(meta_path):
        return get_metadata_from_file(meta_path, post, config, lang, metadata_extractors_by)
    elif lang:
        # Metadata file doesn't exist, but not default language,
        # So, if default language metadata exists, return that.
        # This makes the 2-file format detection more reliable (Issue #525)
        return get_metadata_from_meta_file(meta_path, post, config, None, metadata_extractors_by)
    else:  # No 2-file metadata
        return {}, None


def get_meta(post, lang):
    """Get post meta from compiler or source file."""
    meta = defaultdict(lambda: '')
    used_extractor = None

    config = getattr(post, 'config', None)
    metadata_extractors_by = getattr(post, 'metadata_extractors_by')
    if metadata_extractors_by is None:
        metadata_extractors_by = metadata_extractors.default_metadata_extractors_by()

    # If meta file exists, use it
    metafile_meta, used_extractor = get_metadata_from_meta_file(post.metadata_path, post, config, lang, metadata_extractors_by)

    is_two_file = bool(metafile_meta)

    # Fetch compiler metadata.
    compiler_meta = {}

    if (getattr(post, 'compiler', None) and post.compiler.supports_metadata and
            metadata_extractors.check_conditions(post, post.source_path, post.compiler.metadata_conditions, config, None)):
        compiler_meta = post.compiler.read_metadata(post, lang=lang)
        used_extractor = post.compiler
        meta.update(compiler_meta)

    # Meta files and inter-file metadata override compiler metadata
    if not metafile_meta:
        new_meta, used_extractor = get_metadata_from_file(post.source_path, post, config, lang, metadata_extractors_by)
        meta.update(new_meta)
    else:
        meta.update(metafile_meta)

    # Filename-based metadata extractors (fallback only)
    if not meta:
        extractors = metadata_extractors_by['source'].get(metadata_extractors.MetaSource.filename, [])
        for extractor in extractors:
            if not metadata_extractors.check_conditions(post, post.source_path, extractor.conditions, config, None):
                continue
            meta.update(extractor.extract_filename(post.source_path, lang))

    if lang is None:
        # Only perform these checks for the default language
        if 'slug' not in meta:
            # If no slug is found in the metadata use the filename
            meta['slug'] = slugify(unicode_str(os.path.splitext(
                os.path.basename(post.source_path))[0]), post.default_lang)

        if 'title' not in meta:
            # If no title is found, use the filename without extension
            meta['title'] = os.path.splitext(
                os.path.basename(post.source_path))[0]

    # Set one-file status basing on default language only (Issue #3191)
    if is_two_file or lang is None:
        post.is_two_file = is_two_file

    return meta, used_extractor


def hyphenate(dom, _lang):
    """Hyphenate a post."""
    # circular import prevention
    from .nikola import LEGAL_VALUES
    lang = None
    if pyphen is not None:
        lang = LEGAL_VALUES['PYPHEN_LOCALES'].get(_lang, pyphen.language_fallback(_lang))
    else:
        utils.req_missing(['pyphen'], 'hyphenate texts', optional=True)
    hyphenator = None
    if pyphen is not None and lang is not None:
        # If pyphen does exist, we tell the user when configuring the site.
        # If it does not support a language, we ignore it quietly.
        try:
            hyphenator = pyphen.Pyphen(lang=lang)
        except KeyError:
            LOGGER.error("Cannot find hyphenation dictoniaries for {0} (from {1}).".format(lang, _lang))
            LOGGER.error("Pyphen cannot be installed to ~/.local (pip install --user).")
    if hyphenator is not None:
        for tag in ('p', 'li', 'span'):
            for node in dom.xpath("//%s[not(parent::pre)]" % tag):
                skip_node = False
                skippable_nodes = ['kbd', 'pre', 'code', 'samp', 'mark', 'math', 'data', 'ruby', 'svg']
                if node.getchildren():
                    for child in node.getchildren():
                        if child.tag in skippable_nodes or (child.tag == 'span' and 'math'
                                                            in child.get('class', [])):
                            skip_node = True
                elif 'math' in node.get('class', []):
                    skip_node = True
                if not skip_node:
                    insert_hyphens(node, hyphenator)
    return dom


def insert_hyphens(node, hyphenator):
    """Insert hyphens into a node."""
    textattrs = ('text', 'tail')
    if isinstance(node, lxml.etree._Entity):
        # HTML entities have no .text
        textattrs = ('tail',)
    for attr in textattrs:
        text = getattr(node, attr)
        if not text:
            continue
        new_data = ' '.join([hyphenator.inserted(w, hyphen='\u00AD')
                             for w in text.split(' ')])
        # Spaces are trimmed, we have to add them manually back
        if text[0].isspace():
            new_data = ' ' + new_data
        if text[-1].isspace():
            new_data += ' '
        setattr(node, attr, new_data)

    for child in node.iterchildren():
        insert_hyphens(child, hyphenator)
