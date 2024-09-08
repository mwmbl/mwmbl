<script>
	import Search from '@/components/custom/Search.svelte';
	import { Button } from '@/components/ui/button';
	import EmojioneMagnifyingGlassTiltedRight from '~icons/emojione/magnifying-glass-tilted-right';
</script>

<div class="flex min-h-screen flex-col">
	<div class="border-t-8 border-pink-400"></div>
	<header
		class="flex h-16 w-full max-w-[80rem] flex-row items-center gap-2 self-center p-8 sm:gap-6"
	>
		<h1 class="mr-auto">mwmbl</h1>
		<Button variant="link" href="/" class="px-2">Home</Button>
		<Button variant="link" href="/" class="px-2">Our Mission</Button>
		<Button variant="link" href="/" class="px-2">The Team</Button>
		<Button variant="link" href="/" class="px-2">Donate</Button>
	</header>
	<main class="flex min-h-[75svh] flex-col items-center justify-center gap-9">
		<h2
			class="justify-self-center px-8 text-center text-4xl font-bold !leading-snug md:text-5xl lg:text-6xl"
		>
			The first
			<strong class="rounded-lg bg-pink-300 px-4 py-2">truly</strong>
			open&nbsp;source<br />
			search engine<EmojioneMagnifyingGlassTiltedRight class="ml-4 inline" />
		</h2>
		<p class="mb-8 max-w-full px-6 text-center text-sm sm:text-base md:max-w-[46rem]">
			Mwmbl (pronounced "mumble") is the first search engine that's completely independent, open
			source, and non-profit.
		</p>
		<Search />
	</main>
</div>
