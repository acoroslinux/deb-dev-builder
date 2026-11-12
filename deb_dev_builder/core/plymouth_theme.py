"""Render the Plymouth theme's configurable visual settings."""
import json
import re


def render_theme_script(source, policy, *, locale=None):
    marker = '// @theme-settings'
    end = '// @theme-settings-end'
    if marker not in source or end not in source:
        raise ValueError('Plymouth theme is missing its settings marker')
    visual = dict(policy['visual'])
    if locale:
        visual.update(policy.get('media_translations', {}).get(locale.split('_')[0].split('.')[0], {}))
    definitions = '\n'.join('theme.' + key + ' = ' + json.dumps(value, ensure_ascii=False) + ';'
                            for key, value in visual.items())
    return re.sub(re.escape(marker) + r'\n.*?' + re.escape(end),
                  lambda _: marker + '\n' + definitions + '\n' + end,
                  source, count=1, flags=re.S)
