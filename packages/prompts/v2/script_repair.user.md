The script you returned breaks the rules below. Return the complete beat sheet and script again with only these problems fixed; keep everything else as it was.

{% for issue in issues %}- {{ issue.path }}: {{ issue.message }}
{% endfor %}
