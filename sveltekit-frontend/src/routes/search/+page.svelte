<script>
	import Search from "@/components/custom/Search.svelte";
    import * as Card from "$lib/components/ui/card";

    let { data } = $props();
</script>

<main class="flex flex-col items-center gap-8">
    <Search query={data.query}/>
    <p></p>
    <div class="flex flex-col gap-4 max-w-2xl px-4">
        {#each data.results as result}
            <a href={result.url} class="group">
                <Card.Root class="p-4 flex flex-col gap-2">
                    <div class="group-hover:underline">
                        {result.url}
                        <span class="italic">—found via {result.source}</span>
                    </div>
                    <Card.Title class="font-medium">
                        {#each result.title as titleSegment}
                            {#if titleSegment.is_bold}
                                <strong>{titleSegment.value}</strong>
                            {:else}
                                {titleSegment.value}
                            {/if}
                        {/each}
                    </Card.Title>
                    <Card.Description>
                        {#each result.extract as extractSegment}
                        {#if extractSegment.is_bold}
                            <strong>{extractSegment.value}</strong>
                        {:else}
                            {extractSegment.value}
                        {/if}
                    {/each}
                    </Card.Description>
                </Card.Root>
            </a>
        {/each}
    </div>
</main>