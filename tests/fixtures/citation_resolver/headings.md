<!-- Markdown heading resolver matching fixtures; data only. -->
# Heading One
###### Heading Six
## Longer Heading
##NoSpace
 Indented prose
####### Too Deep
Plain prose with Embedded Heading text
## Trailing Space  
## Literal (dot.)
MD_SYMBOL: 1
#	Tabbed Heading
## Trailing Tab	
```
# Fenced Heading
```
# After Backticks
~~~`info`
# Tilde Heading
~~~
# After Tildes
``
# Two Backticks
~~
# Two Tildes
x```
# Inline Fence
``~
# Mixed Fence
```bad`info
# Backtick Info
````
# Long Backtick Body
```
# Short Close
~~~~
# Wrong Close
```` trailing
# Text Close
x````
# Inline Close
`````
# Longer Close
    ```
# Space Open
    ```
# Space Close
		~~~
# Tab Open
		~~~
# Tab Close
```
# Space Tail Body
```  
# Space Tail Close
~~~
# Tab Tail Body
~~~	
# Tab Tail Close
~~~~
~~~
# Tilde Short Close
~~~~~
# Tilde Longer Close
```
~~~
# Nested Fence
```
# After Nested
 	 ~~~
# Mixed Open
 	 ~~~ 	 
# Mixed Close
``` a`b
# Spaced Backtick Info
~~~ a~b
# Tilde Tilde Info
~~~
 ~~~
# Nbsp Open
~~~
 ~~~
# Nbsp Close
~~~
~~~
~~~ 
# Nbsp Tail
~~~
# Before ~~~ # Hidden ~~~ # Separator
```` a`b
# Long Backtick Info
```
# Much Longer Body
``````
# Much Longer Close
```~x
# Backtick Tilde Info
```
`````````` a`b
# Very Long Backtick Info
``````````
# Very Long Body
````````````````````````````````````````
# Very Long Close
```x`
# Trailing Backtick Info
```

# Blank Line Body
``` ``
# Fence Tail Body
```
> ```
# Quoted Fence
~~~
```
# Backtick In Tilde Body
~~~
