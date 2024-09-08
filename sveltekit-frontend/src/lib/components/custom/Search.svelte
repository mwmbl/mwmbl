<script>
    import { Input } from '@/components/ui/input';
	import { Button } from '@/components/ui/button';
    import PhMagnifyingGlass from '~icons/ph/magnifying-glass'
</script>

<form class="flex flex-row relative items-center max-w-2xl w-full px-4" action="https://mwmbl.org/" method="get">
    <Input
        type="search"
        name="q"
        placeholder="Search with Mwmbl..."
        class="p-6 text-lg rounded-lg"
    />
    <Button type="submit" class="absolute right-6 w-8 h-8 rounded-full bg-pink-300">
        <PhMagnifyingGlass class="text-black min-w-5 min-h-5"/>
    </Button>
</form>
