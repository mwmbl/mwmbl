<script lang="ts">
	import Search from '@/components/custom/Search.svelte';
	import * as Card from '$lib/components/ui/card';
    import { Switch } from "$lib/components/ui/switch";
    import { Label } from "$lib/components/ui/label";
	import { Skeleton } from '@/components/ui/skeleton';

	let { data } = $props();

    let openInNewTab: boolean | undefined = $state(undefined);
	$effect(() => {
		const value = window.localStorage.getItem("openInNewTab")
		if (value) openInNewTab = JSON.parse(value)
		else openInNewTab = false
	})
    $effect(() => {
        window.localStorage.setItem("openInNewTab", JSON.stringify(openInNewTab))
    })
</script>

<main class="flex flex-col items-center gap-8">
	<Search query={data.query} />
    <div class="flex flex-row items-center justify-start gap-3 max-w-2xl w-full px-4">
		{#if openInNewTab === undefined}
			<Skeleton class="h-[24px] w-[44px] rounded-full bg-input" />
		{:else}
        	<Switch id="newtab-switch" class="switch-fade" bind:checked={openInNewTab} />
		{/if}
        <Label for="newtab-switch">Open results in new tab</Label>
    </div>
    <div class="flex max-w-2xl flex-col gap-4 px-4">
		{#each data.results as result}
			<a href={result.url} class="group" target={openInNewTab ? "_blank" : "_self"}>
				<Card.Root class="flex flex-col gap-2 p-4">
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

<style>
	@keyframes switch-fade {
		0% {
			opacity: 0;
		}
		100% {
			opacity: 1;
		}
	}
	:global(.switch-fade) {
		animation: switch-fade 0.2s ease-in-out;
	}
</style>
