"""Peer property projection safety, not a LiveSync YAML parser oracle.

LiveSync stores Blob bytes without parsing YAML (pinned commonlib
managers/EntryManager/EntryManagerImpls.ts:253-280); its size admission is
services/base/VaultService.ts:73-81. Assertions below define the additional
JSON-facing properties API and verify it does not reject ordinary YAML.
"""
import pytest

from frontmatter import FrontmatterError, get_properties, set_properties, add_tags


def note(raw):
    return '---\n' + raw + '---\nbody\n'


@pytest.mark.parametrize('raw', [
    'a: &a scalar\nb: *a\n',
    'a: &a [1,2]\nb: *a\n',
    'a: &a {x: 1}\nb: *a\n',
    'a: &a [1]\nb: &b [*a]\nc: *b\n',
])
def test_alias_plain_values_and_unrelated_edit(raw):
    props = get_properties(note(raw))
    assert props['a'] == props['b'] or props['b'][0] == props['a']
    edited = set_properties(note(raw), {'new': 'value'})
    assert raw in edited
    assert get_properties(edited)['new'] == 'value'
    tagged = add_tags(note(raw), ['tag'])
    assert raw in tagged
    assert get_properties(tagged)['tags'] == ['tag']


def test_task_d_small_valid_notes():
    assert len(get_properties(note('items: [' + ','.join('0' for _ in range(10001)) + ']\n'))['items']) == 10001
    value = get_properties(note('items: ' + '[' * 34 + '0' + ']' * 34 + '\n'))['items']
    for _ in range(34):
        value = value[0]
    assert value == 0


def test_alias_bomb_preflight_is_typed():
    raw = 'a0: &a0 [one, two]\n'
    for index in range(1, 10):
        raw += f'a{index}: &a{index} [' + ', '.join([f'*a{index-1}'] * 10) + ']\n'
    with pytest.raises(FrontmatterError) as error:
        get_properties(note(raw))
    assert error.value.reason == 'alias expansion'
    assert 'intact and synced' in error.value.public_message
    assert 'raw note text' in error.value.public_message
    assert 'one' not in error.value.public_message


@pytest.mark.parametrize('raw,reason', [
    ('a: &a [*a]\n', 'alias expansion'),
    ('a: ' + '[' * 1000 + '0' + ']' * 1000 + '\n', 'depth'),
    ('private_content: [unclosed\n', 'invalid YAML'),
])
def test_refusal_classes_and_no_note_echo(raw, reason):
    with pytest.raises(FrontmatterError) as error:
        get_properties(note(raw))
    assert error.value.reason == reason
    assert 'private_content' not in error.value.public_message
    assert 'intact and synced' in error.value.public_message
    assert 'properties/tags' in error.value.public_message


def test_exact_unchanged_alias_bytes_on_existing_key_edit():
    raw = 'anchor: &a [one,two] # preserve spacing\ncopy: *a\nedit: old\n'
    changed = set_properties(note(raw), {'edit': 'new'})
    assert 'anchor: &a [one,two] # preserve spacing\ncopy: *a\n' in changed
    assert get_properties(changed)['edit'] == 'new'


def test_expanded_nodes_depth_and_memo():
    from frontmatter import _to_plain, FrontmatterLimits
    shared = [1, 2]
    plain = _to_plain({'a': shared, 'b': shared})
    assert plain['a'] is plain['b']
    with pytest.raises(FrontmatterError) as error:
        _to_plain(list(range(20)), limits=FrontmatterLimits(max_nodes=10))
    assert error.value.reason == 'nodes'
    with pytest.raises(FrontmatterError) as error:
        _to_plain([[[0]]], limits=FrontmatterLimits(max_depth=2))
    assert error.value.reason == 'depth'


def test_pinned_plugin_admits_all_frontmatter_shapes():
    # Native plugin size predicate: VaultService.ts:73-81. Blob ingestion at
    # EntryManagerImpls.ts:253-280 has no YAML parser or alias gate.
    import hashlib
    import json
    from pathlib import Path
    fixture = json.loads((Path(__file__).parent / 'fixtures/frontmatter_admission_upstream.json').read_text())
    assert fixture['upstream_commit'] == '484c164450d61deb2e301582b60cb9b5f9fa977f'
    assert fixture['dependencies'] == []  # native method uses only JS builtins
    generator = Path('scripts/extract_frontmatter_admission_vectors.mjs')
    assert fixture['generator_sha256'] == hashlib.sha256(generator.read_bytes()).hexdigest()
    assert len(fixture['vectors']) == 10
    assert all(not row['too_large'] for row in fixture['vectors'])
    raw = 'items: [' + ','.join('0' for _ in range(10001)) + ']\n'
    row = next(row for row in fixture['vectors'] if row['name'] == 'nodes-10001')
    assert row['input_sha256'] == hashlib.sha256(note(raw).encode()).hexdigest()
    assert row['size'] == len(note(raw).encode())


def test_depth_near_default_ceiling_is_supported():
    value = get_properties(note('items: ' + '[' * 199 + '0' + ']' * 199 + '\n'))['items']
    for _ in range(199):
        value = value[0]
    assert value == 0


def test_alias_bomb_declines_before_roundtrip_construction(monkeypatch):
    import frontmatter
    from ruamel.yaml import YAML
    raw = 'a0: &a0 [one, two]\n'
    for index in range(1, 10):
        raw += f'a{index}: &a{index} [' + ', '.join([f'*a{index-1}'] * 10) + ']\n'
    def forbidden_load(*args, **kwargs):
        pytest.fail('budget preflight must run before round-trip construction')
    monkeypatch.setattr(YAML, 'load', forbidden_load)
    with pytest.raises(FrontmatterError) as error:
        frontmatter.get_properties(note(raw))
    assert error.value.reason == 'alias expansion'


def test_spliced_edit_cannot_leave_a_dangling_alias():
    from frontmatter import delete_properties
    original = note('anchor: &a [one,two]\ncopy: *a\n')
    with pytest.raises(FrontmatterError) as error:
        delete_properties(original, ['anchor'])
    assert error.value.reason == 'alias expansion'
    assert get_properties(original)['copy'] == ['one', 'two']


@pytest.mark.parametrize('raw', [
    'base: &b {x: 1}\n<<: *b\nedit: old\n',
    '? [a, b]\n: value\nedit: old\n',
])
def test_unrelated_merge_and_complex_key_bytes_preserved(raw):
    changed = set_properties(note(raw), {'edit': 'new'})
    assert raw[:raw.index('edit:')] in changed
    assert get_properties(changed)['edit'] == 'new'


def test_flow_root_append_preserves_existing_tokens():
    text = note('{a: [one,two], b: 2}\n')
    changed = set_properties(text, {'new': 'value'})
    assert '{a: [one,two], b: 2,' in changed
    assert get_properties(changed)['new'] == 'value'


def test_new_block_respects_projection_budget():
    from frontmatter import frontmatter_limits, FrontmatterLimits
    token = frontmatter_limits.set(FrontmatterLimits(max_nodes=3))
    try:
        with pytest.raises(FrontmatterError) as error:
            set_properties('body', {'items': list(range(5))})
        assert error.value.reason == 'nodes'
        with pytest.raises(FrontmatterError):
            add_tags('body', ['a', 'b', 'c'])
    finally:
        frontmatter_limits.reset(token)


def test_duplicate_anchors_do_not_emit_source_warnings():
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        props = get_properties(note('a: &x private-marker\nb: &x private-marker\nc: *x\n'))
    assert props['c'] == props['b']
    assert not caught


def test_editing_alias_tags_detaches_from_source_anchor():
    from frontmatter import remove_tags
    raw = 'source: &s [a]\ntags: *s\n'
    changed = add_tags(note(raw), ['b'])
    assert 'source: &s [a]\n' in changed
    assert get_properties(changed)['source'] == ['a']
    assert get_properties(changed)['tags'] == ['a', 'b']
    changed = remove_tags(note(raw), ['a'])
    assert 'source: &s [a]\n' in changed
    assert get_properties(changed)['source'] == ['a']
    assert get_properties(changed)['tags'] == []


def test_editing_anchor_tags_keeps_other_alias_tokens():
    raw = 'tags: &s [a]\ncopy: *s\n'
    changed = add_tags(note(raw), ['b'])
    assert 'copy: *s\n' in changed
    assert get_properties(changed)['copy'] == ['a', 'b']


def test_roundtrip_sequence_does_not_build_per_item_location_dicts():
    from frontmatter import _parse
    data = _parse('items: [' + ','.join(str(i) for i in range(10001)) + ']\n')
    # Coordinates are read from the streaming source-span pass, never the
    # nested sequence .lc index dictionary. Retaining it would scale to
    # millions of Python objects despite a compact JSON property result.
    assert data['items'].lc.data is None


@pytest.mark.parametrize('raw,keys,expected', [
    ('a: |\n  hello\nb: keep\nc: end\n', ['a'], 'b: keep\nc: end\n'),
    ('a:\n- one\n- two\nb: keep\nc: end\n', ['a'], 'b: keep\nc: end\n'),
    ('{a: 1, b: 2, c: 3}\n', ['b', 'c'], '{a: 1}\n'),
    ('{a: 1, b: 2, c: 3}\n', ['a', 'b'], '{c: 3}\n'),
])
def test_delete_spans_keep_surviving_keys(raw, keys, expected):
    from frontmatter import delete_properties
    assert delete_properties(note(raw), keys) == note(expected)


@pytest.mark.parametrize('raw', ['[one, two]\n', 'scalar\n'])
def test_valid_nonmapping_yaml_is_mapping_constraint(raw):
    with pytest.raises(FrontmatterError) as error:
        get_properties(note(raw))
    assert error.value.reason == 'mapping required'
    assert 'invalid YAML' not in error.value.public_message
    assert 'raw note text' in error.value.public_message


def test_nested_anchor_tags_alias_detaches():
    raw = 'source: {nested: &s [a]}\ntags: *s\n'
    changed = add_tags(note(raw), ['b'])
    assert 'source: {nested: &s [a]}\n' in changed
    assert get_properties(changed)['source']['nested'] == ['a']


def test_creation_refusal_never_claims_an_unpublished_note_synced():
    error = FrontmatterError('nodes')
    message = error.for_creation()
    assert 'nodes' in message
    assert 'note creation declined before publication; no note was created' in message
    assert 'intact and synced' not in message
    assert 'LIVESYNC_FRONTMATTER_MAX_NODES' in message
    assert 'raw note text' in message


@pytest.mark.parametrize('raw', ['{a: 1,}\n', '{a: 1, }\n'])
def test_flow_root_trailing_comma_append(raw):
    changed = set_properties(note(raw), {'new': 2})
    assert get_properties(changed) == {'a': 1, 'new': 2}


@pytest.mark.parametrize('raw', [
    '!!str <<: value\nedit: old\n',
    "'<<': value\nedit: old\n",
    'base: &b {x: 1}\n<<: *b\nedit: old\n',
])
def test_literal_merge_spelling_is_distinguished_from_merge(raw):
    changed = set_properties(note(raw), {'edit': 'new'})
    assert raw[:raw.index('edit:')] in changed
    assert get_properties(changed)['edit'] == 'new'


def test_flow_trailing_comma_delete_all():
    from frontmatter import delete_properties
    assert get_properties(delete_properties(note('{a: 1,}\n'), ['a'])) == {}


def test_nested_anchor_rebinding_counts_latest_definition():
    from frontmatter import frontmatter_limits, FrontmatterLimits
    text = note('a: &x {child: &x [1]}\nb: *x\n')
    token = frontmatter_limits.set(FrontmatterLimits(max_nodes=9))
    try:
        assert get_properties(text) == {'a': {'child': [1]}, 'b': [1]}
    finally:
        frontmatter_limits.reset(token)
    token = frontmatter_limits.set(FrontmatterLimits(max_nodes=8))
    try:
        with pytest.raises(FrontmatterError) as error:
            get_properties(text)
        assert error.value.reason == 'alias expansion'
    finally:
        frontmatter_limits.reset(token)


@pytest.mark.parametrize('value', [
    'old # deleted inline annotation\n',
    '|\n  # scalar content\n',
    '\n- one\n- two\n',
    '\n  nested: value\n',
])
def test_delete_preserves_following_standalone_comment_bytes(value):
    from frontmatter import delete_properties
    prefix = 'anchor: &a [one,two]\n'
    suffix = '# copy annotation\n\n# second comment\ncopy: *a\n'
    original = note(prefix + 'edit: ' + value + suffix)
    assert delete_properties(original, ['edit']) == note(prefix + suffix)


def test_delete_last_key_preserves_trailing_standalone_comments():
    from frontmatter import delete_properties
    original = note('keep: value\nedit: old # inline\n# trailing annotation\n')
    assert delete_properties(original, ['edit']) == note('keep: value\n# trailing annotation\n')


@pytest.mark.parametrize('value', ['old\n', '\n- one\n- two\n', '\n  nested: value\n'])
def test_delete_preserves_indented_yaml_comment_tokens(value):
    from frontmatter import delete_properties
    prefix = 'anchor: &a [one,two]\n'
    suffix = '  # copy annotation\n\n  # another annotation\ncopy: *a\n'
    assert delete_properties(note(prefix + 'edit: ' + value + suffix), ['edit']) == note(prefix + suffix)


@pytest.mark.parametrize('raw', [
    'base: &big ' + 'x' * 32768 + '\n' + ''.join(f'copy{i}: *big\n' for i in range(2048)),
    'base: &big [' + ','.join(['x' * 1024] * 32) + ']\n' + ''.join(f'copy{i}: *big\n' for i in range(128)),
    'base: &big {key: ' + 'x' * 32768 + '}\n' + ''.join(f'copy{i}: *big\n' for i in range(128)),
])
def test_expanded_scalar_bytes_refuse_before_construction(raw, monkeypatch):
    import frontmatter
    def construct(*args, **kwargs):
        pytest.fail('expanded byte refusal must precede YAML construction')
    monkeypatch.setattr(frontmatter.RoundTripConstructor, 'construct_document', construct)
    with pytest.raises(FrontmatterError) as error:
        get_properties(note(raw))
    assert error.value.reason == 'bytes'
    assert 'LIVESYNC_FRONTMATTER_MAX_BYTES' in str(error.value)
    assert 'raw note text' in str(error.value)


@pytest.mark.parametrize('shape', ['scalar', 'sequence', 'mapping'])
def test_expanded_utf8_bytes_exact_boundary(shape):
    from frontmatter import FrontmatterLimits, frontmatter_limits, _to_plain
    value = 'é' * 10
    node = value if shape == 'scalar' else [value] if shape == 'sequence' else {'k': value}
    tree = {'a': node, 'b': node}
    size = 42 if shape != 'mapping' else 44  # UTF-8 strings and keys at every occurrence
    token = frontmatter_limits.set(FrontmatterLimits(max_bytes=size))
    try:
        assert _to_plain(tree) == tree
    finally:
        frontmatter_limits.reset(token)
    token = frontmatter_limits.set(FrontmatterLimits(max_bytes=size - 1))
    try:
        with pytest.raises(FrontmatterError, match='frontmatter bytes'):
            _to_plain(tree)
    finally:
        frontmatter_limits.reset(token)


@pytest.mark.parametrize('raw', ['a: &x ééé\nb: *x\n', 'a: &x [ééé]\nb: *x\n', 'a: &x {k: ééé}\nb: *x\n'])
def test_yaml_alias_bytes_at_and_over_boundary(raw):
    from frontmatter import FrontmatterLimits, frontmatter_limits
    size = 16 if '{k:' in raw else 14
    for budget in (size, size - 1):
        token = frontmatter_limits.set(FrontmatterLimits(max_bytes=budget))
        try:
            if budget == size:
                assert get_properties(note(raw))['a'] == get_properties(note(raw))['b']
            else:
                with pytest.raises(FrontmatterError, match='frontmatter bytes'):
                    get_properties(note(raw))
        finally:
            frontmatter_limits.reset(token)


def test_numeric_mapping_keys_count_projected_string_bytes():
    from frontmatter import _to_plain, FrontmatterLimits
    with pytest.raises(FrontmatterError, match='frontmatter bytes'):
        _to_plain({12345: None}, limits=FrontmatterLimits(max_bytes=4))
    assert _to_plain({12345: None}, limits=FrontmatterLimits(max_bytes=5)) == {'12345': None}


def test_default_frontmatter_limits():
    from frontmatter import FrontmatterLimits
    assert FrontmatterLimits().max_nodes == 20_000
    assert FrontmatterLimits().max_depth == 200
    assert FrontmatterLimits().max_bytes == 4 * 1024**2
