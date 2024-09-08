<script>
	import { Button } from '@/components/ui/button';
	import '../app.css';
</script>

<div class="flex min-h-screen flex-col">
	<div class="border-t-8 border-pink-400"></div>
	<header
		class="flex h-16 w-full max-w-[80rem] flex-row items-center gap-2 self-center p-8 sm:gap-6"
	>
		<a href="/" class="mr-auto"><h1>mwmbl</h1></a>
		<Button variant="link" href="/" class="px-2">Home</Button>
		<Button variant="link" href="/" class="px-2">Our Mission</Button>
		<Button variant="link" href="/" class="px-2">The Team</Button>
		<Button variant="link" href="https://opencollective.com/mwmbl" class="px-2">Donate</Button>
	</header>
	<slot></slot>
</div>
