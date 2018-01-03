from itertools import chain
import pickle
from types import FunctionType, GeneratorType

from cloudpickle import CloudPickler

from ._core import private_frame_data, restore_frame, unset_value


__version__ = '0.1.0'


def _empty_cell():
    """Create an empty cell.
    """
    if False:
        free = None

    return (lambda: free).__closure__[0]


def _make_cell(f_locals, var):
    """Create a PyCell object around a value.
    """
    value = f_locals.get(var, unset_value)
    if value is unset_value:
        # unset the name ``value`` to return an empty cell
        del value

    return (lambda: value).__closure__[0]


def _fill_generator(gen, lasti, f_locals, frame_data):
    """Reconstruct a generator instance.

    Parameters
    ----------
    gen : generator
        The skeleton generator.
    lasti : int
        The last instruction executed in the generator. -1 indicates that the
        generator hasn't been started.
    f_locals : dict
        The values of the locals at current execution step.
    frame_data : tuple
        The extra frame data extracted from ``private_frame_data``.

    Returns
    -------
    gen : generator
        The filled generator instance.
    """
    code = gen.gi_frame.f_code
    locals_ = [f_locals.get(var, unset_value) for var in code.co_varnames]
    locals_.extend(
        _make_cell(f_locals, var)
        for var in chain(code.co_cellvars, code.co_freevars)
    )
    restore_frame(gen.gi_frame, lasti, locals_, *frame_data)
    return gen


def _create_skeleton_generator(gen_func):
    """Create an instance of a generator from a generator function without
    the proper stack, locals, or closure.

    Parameters
    ----------
    gen_func : function
        The function to call to create the instance.

    Returns
    -------
    skeleton_generator : generator
        The uninitialized generator instance.
    """
    code = gen_func.__code__
    kwonly_names = code.co_varnames[code.co_argcount:code.co_kwonlyargcount]
    gen = gen_func(
        *(None for _ in range(code.co_argcount)),
        **{key: None for key in kwonly_names}
    )

    # manually update the qualname to fix a bug in Python 3.6 where the
    # qualname is not correct when using both *args and **kwargs
    gen.__qualname__ = gen_func.__qualname__

    return gen


def _restore_spent_generator(name, qualname):
    """Reconstruct a fully consumed generator.

    Parameters
    ----------
    name : str
        The name of the fully consumed generator.
    name : str
        The qualname of the fully consumed generator.

    Returns
    -------
    gen : generator
        A generator which has been fully consumed.
    """
    def single_generator():
        # we actually need to run the gen to ensure that gi_frame gets
        # deallocated to further match the behavior of the existing generator;
        # this is why we do not just do: ``if False: yield``
        yield

    single_generator.__name__ = name
    single_generator.__qualname__ = qualname

    gen = single_generator()
    next(gen)
    return gen


def _save_generator(self, gen):
    frame = gen.gi_frame
    if frame is None:
        # frame is None when the generator is fully consumed; take a fast path
        self.save_reduce(
            _restore_spent_generator,
            (gen.__name__, gen.__qualname__),
            obj=gen,
        )
        return

    f_locals = frame.f_locals
    f_code = frame.f_code

    # Create a copy of generator function without the closure to serve as a box
    # to serialize the code, globals, name, and closure. Cloudpickle already
    # handles things like closures and complicated globals so just rely on
    # cloudpickle to serialize this function.
    gen_func = FunctionType(
        f_code,
        frame.f_globals,
        gen.__name__,
        (),
        (_empty_cell(),) * len(f_code.co_freevars),
    )
    gen_func.__qualname__ = gen.__qualname__

    save = self.save
    write = self.write

    # push a function onto the stack to fill up our skeleton generator
    save(_fill_generator)

    # the start of the tuple to pass to ``_fill_generator``
    write(pickle.MARK)

    save(_create_skeleton_generator)
    save((gen_func,))
    write(pickle.REDUCE)
    self.memoize(gen)

    # push the rest of the arguments to ``_fill_generator``
    save(frame.f_lasti)
    save(f_locals)
    save(private_frame_data(frame))

    # call ``_fill_generator``
    write(pickle.TUPLE)
    write(pickle.REDUCE)


def register():
    """Register the cloudpickle extension.
    """
    CloudPickler.dispatch[GeneratorType] = _save_generator


def unregister():
    """Unregister the cloudpickle extension.
    """
    if CloudPickler.dispatch.get(GeneratorType) is _save_generator:
        # make sure we are only removing the dispatch we added, not someone
        # else's
        del CloudPickler.dispatch[GeneratorType]