########################################################################
# File name: caps115.py
# This file is part of: aioxmpp
#
# LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
########################################################################
import base64
import hashlib

from xml.sax.saxutils import escape


def build_identities_string(identities):
    identities = [
        b"/".join([
            escape(identity.category).encode("utf-8"),
            escape(identity.type_).encode("utf-8"),
            escape(str(identity.lang or "")).encode("utf-8"),
            escape(identity.name or "").encode("utf-8"),
        ])
        for identity in identities
    ]

    if len(set(identities)) != len(identities):
        raise ValueError("duplicate identity")

    identities.sort()
    identities.append(b"")
    return b"<".join(identities)


def build_features_string(features):
    features = list(escape(feature).encode("utf-8") for feature in features)

    if len(set(features)) != len(features):
        raise ValueError("duplicate feature")

    features.sort()
    features.append(b"")
    return b"<".join(features)


def build_forms_string(forms):
    types = set()
    forms_list = []
    for form in forms:
        try:
            form_types = set(
                value
                for field in form.fields.filter(attrs={"var": "FORM_TYPE"})
                for value in field.values
            )
        except KeyError:
            continue

        if len(form_types) > 1:
            raise ValueError("form with multiple types")
        elif not form_types:
            continue

        type_ = escape(next(iter(form_types))).encode("utf-8")
        if type_ in types:
            raise ValueError("multiple forms of type {!r}".format(type_))
        types.add(type_)
        forms_list.append((type_, form))
    forms_list.sort()

    parts = []

    for type_, form in forms_list:
        parts.append(type_)

        field_list = sorted(
            (
                (escape(field.var).encode("utf-8"), field.values)
                for field in form.fields
                if field.var != "FORM_TYPE"
            ),
            key=lambda x: x[0]
        )

        for var, values in field_list:
            parts.append(var)
            parts.extend(sorted(
                escape(value).encode("utf-8") for value in values
            ))

    parts.append(b"")
    return b"<".join(parts)


def hash_query(query, algo):
    hashimpl = hashlib.new(algo)
    hashimpl.update(
        build_identities_string(query.identities)
    )
    hashimpl.update(
        build_features_string(query.features)
    )
    hashimpl.update(
        build_forms_string(query.exts)
    )

    return base64.b64encode(hashimpl.digest()).decode("ascii")
