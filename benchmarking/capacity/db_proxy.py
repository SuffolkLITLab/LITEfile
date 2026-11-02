"""TCP latency approximation; no SQL changes or connection pooling."""

import asyncio


async def connection(reader, writer):
    upstream_reader, upstream_writer = await asyncio.open_connection("db", 5432)
    await asyncio.sleep(0.04)

    async def pump(source, destination):
        try:
            while data := await source.read(65536):
                await asyncio.sleep(0.005)
                destination.write(data)
                await destination.drain()
        finally:
            destination.close()

    await asyncio.gather(
        pump(reader, upstream_writer),
        pump(upstream_reader, writer),
        return_exceptions=True,
    )


async def main():
    server = await asyncio.start_server(connection, "0.0.0.0", 5432)
    async with server:
        await server.serve_forever()


asyncio.run(main())
