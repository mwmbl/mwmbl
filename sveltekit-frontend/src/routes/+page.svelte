<script>
	import Search from '@/components/custom/Search.svelte';
	import { Button } from '@/components/ui/button';
	import EmojioneMagnifyingGlassTiltedRight from '~icons/emojione/magnifying-glass-tilted-right'
</script>

<div class="flex min-h-screen flex-col">
    <div class="border-t-8 border-pink-400"></div>
	<header class="flex flex-row sm:gap-6 gap-2 h-16 p-8 max-w-[80rem] w-full self-center items-center">
		<h1 class="mr-auto">mwmbl</h1>
        <Button variant="link" href="/" class="px-2">Home</Button>
        <Button variant="link" href="/" class="px-2">Our Mission</Button>
        <Button variant="link" href="/" class="px-2">The Team</Button>
        <Button variant="link" href="/" class="px-2">Donate</Button>
	</header>
	<main class="flex min-h-[75svh] flex-col items-center justify-center gap-9">
		<h2 class="justify-self-center text-center lg:text-6xl md:text-5xl text-4xl !leading-snug font-bold px-8">
			The first
			<strong class="bg-pink-300 py-2 px-4 rounded-lg">truly</strong>
			open&nbsp;source<br>
			search engine<EmojioneMagnifyingGlassTiltedRight class="inline ml-4"/>
		</h2>
		<p class="md:max-w-[46rem] max-w-full px-6 text-center mb-8 sm:text-base text-sm">
			Mwmbl (pronounced "mumble") is the first search engine that's completely independent, open
			source, and non-profit.
		</p>
        <Search/>
	</main>
</div>
