#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""CONFIGURE statement compilation functions."""


import typing

from edb import errors

from edb.ir import ast as irast
from edb.ir import staeval as ireval
from edb.ir import typeutils as irtyputils

from edb.schema import links as s_links
from edb.schema import types as s_types

from edb.edgeql import ast as qlast

from . import context
from . import dispatch
from . import setgen


@dispatch.compile.register
def compile_ConfigSet(
        expr: qlast.ConfigSet, *, ctx: context.ContextLevel) -> irast.Set:

    param_name, _ = _validate_op(expr, ctx=ctx)
    param_val = dispatch.compile(expr.expr, ctx=ctx)

    try:
        ireval.evaluate(param_val, schema=ctx.env.schema)
    except ireval.UnsupportedExpressionError:
        level = 'SYSTEM' if expr.system else 'SESSION'
        raise errors.QueryError(
            f'non-constant expression in CONFIGURE {level} SET',
            context=expr.expr.context
        ) from None

    return irast.ConfigSet(
        name=param_name,
        system=expr.system,
        context=expr.context,
        expr=param_val,
    )


@dispatch.compile.register
def compile_ConfigReset(
        expr: qlast.ConfigReset, *, ctx: context.ContextLevel) -> irast.Set:
    param_name, param_type = _validate_op(expr, ctx=ctx)
    filter_expr = expr.where

    if not param_type.is_object_type() and filter_expr is not None:
        raise errors.QueryError(
            'RESET of a primitive configuration parameter '
            'must not have a FILTER clause',
            context=expr.context,
        )

    elif param_type.is_object_type():
        param_type_name = param_type.get_name(ctx.env.schema)
        param_type_ref = qlast.ObjectRef(
            name=param_type_name.name,
            module=param_type_name.module,
        )
        select = qlast.SelectQuery(
            result=qlast.Shape(
                expr=qlast.Path(steps=[param_type_ref]),
                elements=get_config_type_shape(
                    ctx.env.schema, param_type, path=[param_type_ref]),
            ),
            where=filter_expr,
        )

        ctx.modaliases[None] = 'cfg'
        select_ir = dispatch.compile(select, ctx=ctx)

    return irast.ConfigReset(
        name=param_name,
        system=expr.system,
        context=expr.context,
        selector=select_ir,
    )


@dispatch.compile.register
def compile_ConfigInsert(
        expr: qlast.ConfigInsert, *, ctx: context.ContextLevel) -> irast.Set:

    param_name, _ = _validate_op(expr, ctx=ctx)

    if not expr.system:
        raise errors.UnsupportedFeatureError(
            f'CONFIGURE SESSION INSERT is not supported'
        )

    level = 'SYSTEM' if expr.system else 'SESSION'
    schema = ctx.env.schema
    subject = schema.get(f'cfg::{expr.name.name}', None)
    if subject is None:
        raise errors.ConfigurationError(
            f'{expr.name.name!r} is not a valid configuration item',
            context=expr.context,
        )

    insert_stmt = qlast.InsertQuery(
        subject=qlast.Path(
            steps=[
                qlast.ObjectRef(
                    name=expr.name.name,
                    module='cfg',
                )
            ]
        ),
        shape=expr.shape,
    )

    for el in expr.shape:
        if isinstance(el.compexpr, qlast.InsertQuery):
            _inject_tname(el.compexpr, ctx=ctx)

    with ctx.newscope() as subctx:
        subctx.expr_exposed = True
        subctx.modaliases = ctx.modaliases.copy()
        subctx.modaliases[None] = 'cfg'
        subctx.special_computables_in_mutation_shape |= {'_tname'}
        insert_ir = dispatch.compile(insert_stmt, ctx=subctx)
        insert_subject = insert_ir.expr.subject

        _validate_config_object(insert_subject, level=level, ctx=subctx)

    return setgen.ensure_set(
        irast.ConfigInsert(
            name=param_name,
            system=expr.system,
            expr=insert_subject,
            context=expr.context,
        ),
        ctx=ctx,
    )


def _inject_tname(
        insert_stmt: qlast.InsertQuery, *,
        ctx: context.ContextLevel) -> None:

    for el in insert_stmt.shape:
        if isinstance(el.compexpr, qlast.InsertQuery):
            _inject_tname(el.compexpr, ctx=ctx)

    insert_stmt.shape.append(
        qlast.ShapeElement(
            expr=qlast.Path(
                steps=[qlast.Ptr(ptr=qlast.ObjectRef(name='_tname'))],
            ),
            compexpr=qlast.Path(
                steps=[
                    qlast.Introspect(
                        type=qlast.TypeName(
                            maintype=insert_stmt.subject.steps[0],
                        ),
                    ),
                    qlast.Ptr(ptr=qlast.ObjectRef(name='name')),
                ],
            ),
        ),
    )


def _validate_config_object(
        expr: irast.Set, *,
        level: str,
        ctx: context.ContextLevel) -> None:

    for element in expr.shape:
        if element.rptr.ptrref.shortname.name == 'id':
            continue

        if (irtyputils.is_object(element.typeref)
                and isinstance(element.expr, irast.InsertStmt)):
            _validate_config_object(element, level=level, ctx=ctx)


def _validate_op(
        expr: qlast.ConfigOp, *,
        ctx: context.ContextLevel) -> typing.Tuple[str, s_types.Type]:

    if expr.name.module and expr.name.module != 'cfg':
        raise errors.QueryError(
            'invalid configuration parameter name: module must be either '
            '\'cfg\' or empty', context=expr.name.context,
        )

    name = expr.name.name
    cfg_host_type = ctx.env.schema.get('cfg::Config')
    cfg_type = None

    if isinstance(expr, (qlast.ConfigSet, qlast.ConfigReset)):
        # expr.name is the actual name of the property.
        ptr = cfg_host_type.getptr(ctx.env.schema, name)
        if ptr is not None:
            cfg_type = ptr.get_target(ctx.env.schema)

    if cfg_type is None:
        if isinstance(expr, qlast.ConfigSet):
            raise errors.ConfigurationError(
                f'unrecognized configuration parameter {name!r}',
                context=expr.context
            )

        # expr.name is the name of the configuration type
        cfg_type = ctx.env.schema.get(f'cfg::{name}', None)
        if cfg_type is None:
            raise errors.ConfigurationError(
                f'unrecognized configuration object {name!r}',
                context=expr.context
            )

        ptr = None

        mro = [cfg_type] + list(
            cfg_type.get_mro(ctx.env.schema).objects(ctx.env.schema))
        for ct in mro:
            ptrs = ctx.env.schema.get_referrers(
                ct, scls_type=s_links.Link, field_name='target')

            if ptrs:
                ptr = next(iter(ptrs))
                break

        if ptr is None or ptr.get_source(ctx.env.schema) != cfg_host_type:
            raise errors.ConfigurationError(
                f'{name!r} cannot be configured directly'
            )

        name = ptr.get_shortname(ctx.env.schema).name

    sys_attr = ptr.get_attributes(ctx.env.schema).get(
        ctx.env.schema, 'cfg::system', None)

    system = (
        sys_attr is not None
        and sys_attr.get_value(ctx.env.schema) == 'true'
    )

    if not expr.system and system:
        raise errors.ConfigurationError(
            f'{name!r} is a system-level configuration parameter; '
            f'use "CONFIGURE SYSTEM"')

    return name, cfg_type


def get_config_type_shape(
        schema, stype, path) -> typing.List[qlast.ShapeElement]:
    shape = []
    seen = set()

    stypes = [stype] + list(stype.descendants(schema))

    for t in stypes:
        t_name = t.get_name(schema)

        for pn, p in t.get_pointers(schema).items(schema):
            if pn in ('id', '__type__') or pn in seen:
                continue

            elem_path = []

            if t is not stype:
                elem_path.append(
                    qlast.TypeIndirection(
                        type=qlast.TypeName(
                            maintype=qlast.ObjectRef(
                                module=t_name.module,
                                name=t_name.name,
                            ),
                        ),
                    ),
                )

            elem_path.append(qlast.Ptr(ptr=qlast.ObjectRef(name=pn)))

            ptype = p.get_target(schema)

            if ptype.is_object_type():
                subshape = get_config_type_shape(
                    schema, ptype, path + elem_path)
                subshape.append(
                    qlast.ShapeElement(
                        expr=qlast.Path(
                            steps=[
                                qlast.Ptr(
                                    ptr=qlast.ObjectRef(name='_tname'),
                                ),
                            ],
                        ),
                        compexpr=qlast.Path(
                            steps=path + elem_path + [
                                qlast.Ptr(
                                    ptr=qlast.ObjectRef(name='__type__')),
                                qlast.Ptr(
                                    ptr=qlast.ObjectRef(name='name')),
                            ],
                        ),
                    ),
                )
            else:
                subshape = []

            shape.append(
                qlast.ShapeElement(
                    expr=qlast.Path(steps=elem_path),
                    elements=subshape,
                ),
            )

            seen.add(pn)

    return shape
