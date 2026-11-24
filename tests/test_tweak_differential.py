"""429 randomized pairs, twice each, from real pinned Node upstream modules."""
import gzip
import json
from pathlib import Path
import pytest
import livesync_format as fmt
from content_splitter import normalize_splitter_settings

FIXTURE = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures' /
    'tweak_compatibility_upstream.json.gz').read_bytes()))


def value_dict(value):
    result = {'present': value.present}
    if value.raw_value is not fmt._TWEAK_UNDEFINED:
        result['rawValue'] = value.raw_value
    if value.effective_value is not fmt._TWEAK_UNDEFINED:
        result['effectiveValue'] = value.effective_value
    return result


def transition_dict(transition):
    return {'changes': dict(transition.changes), 'reconstruction': transition.reconstruction,
        'reasons': [dict(key=key, reconstruction=reconstruction) for key,reconstruction in transition.reasons]}


def upstream_shape(assessment):
    return {'alignment': assessment.alignment,
        'representationDiffers': assessment.representation_differs,
        'entries': [dict(key=e.key, current=value_dict(e.current), preferred=value_dict(e.preferred),
                         relation=e.relation) for e in assessment.entries],
        'adoptPreferred': transition_dict(assessment.adopt_preferred),
        'adoptCurrent': transition_dict(assessment.adopt_current),
        'onlyCompatibleLossyDifferences': assessment.only_compatible_lossy_differences,
        'currentValues': dict(assessment.current_values), 'preferredValues': dict(assessment.preferred_values)}


@pytest.mark.parametrize('case', FIXTURE['vectors'], ids=lambda c: None)
def test_pinned_assessment(case):
    assert normalize_splitter_settings(case['current']) == case['normalizedCurrent']
    assert normalize_splitter_settings(case['preferred']) == case['normalizedPreferred']
    normalized = fmt.assess_tweak_compatibility(case['preferred'], case['current'])
    assert upstream_shape(normalized) == case['expected']
    # Also check all original upstream vectors BEFORE migration. In particular
    # this pins the declared default for an absent chunkSplitterVersion.
    raw = fmt._assess_normalized_tweaks(case['preferred'], case['current'])
    assert upstream_shape(raw) == case['rawExpected']


@pytest.mark.parametrize('case', FIXTURE['policies'])
def test_pinned_plugin_policy(case):
    assessment = fmt.assess_tweak_compatibility(case['preferred'], case['current'])
    enabled = case.get('flag', True) is True
    side = fmt.select_newer_tweak_side(case['current'], case['preferred']) if (
        enabled and assessment.only_compatible_lossy_differences) else None
    assert side == case['winner']


def test_pinned_rule_inventory():
    actual = []
    for rule in fmt.TWEAK_COMPATIBILITY_RULES:
        item = dict(key=rule.key, templateValue=rule.template_value, reconstruction=rule.reconstruction)
        if rule.known_default is not fmt._TWEAK_UNDEFINED:
            item['knownDefault'] = rule.known_default
        if rule.compatible_lossy_order is not None:
            item['compatibleLossyOrder'] = rule.compatible_lossy_order
        actual.append(item)
    assert actual == FIXTURE['rules']


@pytest.mark.parametrize('case', FIXTURE['numberVectors'])
def test_pinned_binary64_boundary(case):
    current, preferred = json.loads(case['inputJson'])
    assessment = fmt.assess_tweak_compatibility(preferred, current)
    assert upstream_shape(assessment) == case['expected']
    winner = fmt.select_newer_tweak_side(current, preferred) if assessment.only_compatible_lossy_differences else None
    assert winner == case['winner']
