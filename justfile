next_version := `git-cliff --bumped-version`

create-changelog:
    git-cliff --bump -o CHANGELOG.md

release: create-changelog
    awk -i inplace '{ sub(/^version = "[0-9]+\.[0-9]+\.[0-9]+"/, "version = \"{{next_version}}\"") }; { print }' pyproject.toml
    awk -i inplace '{ sub(/^__version__ = "[0-9]+\.[0-9]+\.[0-9]+"/, "__version__ = \"{{next_version}}\"") }; { print }' aiobp/__init__.py
    uv sync
    git add --ignore-errors pyproject.toml uv.lock CHANGELOG.md aiobp/__init__.py
    git commit -m {{next_version}}
    git tag -a {{next_version}} -m {{next_version}}

publish:
    rm dist/*
    uv build
    uv publish

push:
    git push
    git push --tags

test:
    uv run -m unittest discover -s tests
