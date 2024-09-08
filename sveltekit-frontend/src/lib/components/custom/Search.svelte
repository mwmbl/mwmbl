<script>
	import { Input } from '@/components/ui/input';
	import { Button } from '@/components/ui/button';
	import PhMagnifyingGlass from '~icons/ph/magnifying-glass';

	let { query = null } = $props(); 
</script>

<form
	class="relative flex w-full max-w-[46rem] flex-row items-center px-4"
	action="/search"
	method="get"
>
	<Input type="search" name="q" placeholder="Search with Mwmbl..." class="rounded-lg p-6 text-lg" value={query} />
	<Button type="submit" class="absolute right-6 h-8 w-8 rounded-full bg-pink-300">
		<PhMagnifyingGlass class="min-h-5 min-w-5 text-black" />
	</Button>
</form>
